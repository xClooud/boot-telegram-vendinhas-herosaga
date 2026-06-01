const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { execFile } = require('child_process');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const SHOP_FILE = path.join(PROJECT_ROOT, 'config', 'shop_urls.txt');
const ENV_FILE = path.join(PROJECT_ROOT, '.env');

function ensureFiles() {
  const configDir = path.join(PROJECT_ROOT, 'config');
  if (!fs.existsSync(configDir)) {
    fs.mkdirSync(configDir, { recursive: true });
  }

  if (!fs.existsSync(SHOP_FILE)) {
    fs.writeFileSync(
      SHOP_FILE,
      '# Formato: nome|url|moeda\n# Se não quiser nome, pode deixar só a URL.\n# Moeda opcional: Zeny, Hero Points ou Moeda RMT.\n',
      'utf8'
    );
  }

  if (!fs.existsSync(ENV_FILE)) {
    fs.writeFileSync(ENV_FILE, '', 'utf8');
  }
}

function parseShopLine(line) {
  const clean = line.trim();
  if (!clean || clean.startsWith('#')) {
    return null;
  }
  if (clean.includes('|')) {
    const parts = clean.split('|').map((part) => part.trim());
    const [name = '', url = '', currency = ''] = parts;
    return { name, url, currency };
  }
  return { name: '', url: clean, currency: '' };
}

function loadShops() {
  ensureFiles();
  const lines = fs.readFileSync(SHOP_FILE, 'utf8').split(/\r?\n/);
  const shops = [];
  for (const line of lines) {
    const parsed = parseShopLine(line);
    if (parsed?.url) {
      shops.push(parsed);
    }
  }
  return shops;
}

function saveShops(shops) {
  const lines = [
    '# Formato: nome|url|moeda',
    '# Se não quiser nome, pode deixar só a URL.',
    '# Moeda opcional: Zeny, Hero Points ou Moeda RMT.',
    ''
  ];

  for (const shop of shops) {
    const name = (shop.name || '').trim();
    const url = (shop.url || '').trim();
    const currency = (shop.currency || '').trim();
    if (!url) {
      continue;
    }
    if (name || currency) {
      lines.push(`${name}|${url}|${currency}`);
    } else {
      lines.push(url);
    }
  }

  fs.writeFileSync(SHOP_FILE, `${lines.join('\n').trim()}\n`, 'utf8');
}

function normalizeShop(shop) {
  return {
    name: (shop?.name || '').trim(),
    url: (shop?.url || '').trim(),
    currency: (shop?.currency || '').trim()
  };
}

function parseEnv(content) {
  const env = {};
  const lines = content.split(/\r?\n/);
  let currentKey = null;
  let currentParts = [];

  function flushCurrent() {
    if (currentKey) {
      env[currentKey] = currentParts.join('\n').replace(/\n+$/, '');
    }
    currentKey = null;
    currentParts = [];
  }

  for (const line of lines) {
    const clean = line.trim();
    if (!clean || clean.startsWith('#')) {
      flushCurrent();
      continue;
    }
    const sep = clean.indexOf('=');
    if (sep === -1) {
      if (currentKey) {
        currentParts.push(line);
      }
      continue;
    }
    flushCurrent();
    currentKey = clean.slice(0, sep).trim();
    currentParts = [line.slice(sep + 1)];
  }

  flushCurrent();
  return env;
}

function loadEnv() {
  ensureFiles();
  const content = fs.readFileSync(ENV_FILE, 'utf8');
  return parseEnv(content);
}

function saveEnv(input) {
  const current = loadEnv();
  const next = {
    ...current,
    TOKEN: input.TOKEN ?? current.TOKEN ?? '',
    CHAT_ID: input.CHAT_ID ?? current.CHAT_ID ?? '',
    DISCORD_WEBHOOK: input.DISCORD_WEBHOOK ?? current.DISCORD_WEBHOOK ?? '',
    TELEGRAM_MESSAGE: input.TELEGRAM_MESSAGE ?? current.TELEGRAM_MESSAGE ?? '',
    DISCORD_MESSAGE: input.DISCORD_MESSAGE ?? current.DISCORD_MESSAGE ?? '',
    CHAT_IDS: input.CHAT_IDS ?? current.CHAT_IDS ?? '',
    VPS_SSH_TARGET: input.VPS_SSH_TARGET ?? current.VPS_SSH_TARGET ?? '',
    VPS_SSH_KEY_PATH: input.VPS_SSH_KEY_PATH ?? current.VPS_SSH_KEY_PATH ?? '',
    VPS_PROJECT_DIR: input.VPS_PROJECT_DIR ?? current.VPS_PROJECT_DIR ?? '/home/ubuntu/boot-telegram-vendinhas-herosaga'
  };

  const lines = [
    '# Bot',
    `TOKEN=${next.TOKEN}`,
    `CHAT_ID=${next.CHAT_ID}`,
    `CHAT_IDS=${next.CHAT_IDS}`,
    `TELEGRAM_MESSAGE=${next.TELEGRAM_MESSAGE}`,
    `DISCORD_WEBHOOK=${next.DISCORD_WEBHOOK}`,
    `DISCORD_MESSAGE=${next.DISCORD_MESSAGE}`,
    '',
    '# VPS sync',
    `VPS_SSH_TARGET=${next.VPS_SSH_TARGET}`,
    `VPS_SSH_KEY_PATH=${next.VPS_SSH_KEY_PATH}`,
    `VPS_PROJECT_DIR=${next.VPS_PROJECT_DIR}`,
    ''
  ];

  fs.writeFileSync(ENV_FILE, lines.join('\n'), 'utf8');
  return next;
}

function runCommand(bin, args, cwd, extra = {}) {
  return new Promise((resolve) => {
    execFile(bin, args, { cwd, windowsHide: true, env: { ...process.env, ...extra.env } }, (error, stdout, stderr) => {
      resolve({
        ok: !error,
        code: error?.code ?? 0,
        output: [stdout, stderr].filter(Boolean).join('\n').trim()
      });
    });
  });
}

async function getCurrentGitBranch() {
  const result = await runCommand('git', ['branch', '--show-current'], PROJECT_ROOT);
  const branch = (result.output || '').trim();
  return branch || 'HEAD';
}

function buildPythonEnv(input = {}, extras = {}) {
  const env = { ...extras };
  const fields = [
    'TOKEN',
    'CHAT_ID',
    'CHAT_IDS',
    'TELEGRAM_MESSAGE',
    'DISCORD_WEBHOOK',
    'DISCORD_MESSAGE',
    'NOTIFY_COOLDOWN',
    'REQUEST_TIMEOUT',
    'SHOP_URL',
    'SHOP_URLS'
  ];

  for (const field of fields) {
    const value = input[field];
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      env[field] = String(value);
    }
  }

  if (input.BOT_TOKEN && !env.TOKEN) {
    env.TOKEN = String(input.BOT_TOKEN).trim();
  }

  if (input.TELEGRAM_CHAT_ID && !env.CHAT_ID) {
    env.CHAT_ID = String(input.TELEGRAM_CHAT_ID).trim();
  }

  return env;
}

function buildSaveResponse(shops, env, logs = ['Configuração salva.']) {
  return {
    ok: true,
    shopsCount: shops.length,
    env,
    logs
  };
}

async function syncToVps(options) {
  const logs = [];
  const currentBranch = await getCurrentGitBranch();

  const add = await runCommand('git', ['add', 'config/shop_urls.txt'], PROJECT_ROOT);
  logs.push(`git add: ${add.ok ? 'OK' : 'FALHOU'}`);
  if (add.output) logs.push(add.output);
  if (!add.ok) return { ok: false, logs };

  const diff = await runCommand('git', ['diff', '--cached', '--quiet'], PROJECT_ROOT);
  if (!diff.ok) {
    const commit = await runCommand('git', ['commit', '-m', 'Update shop URLs via desktop app'], PROJECT_ROOT);
    logs.push(`git commit: ${commit.ok ? 'OK' : 'FALHOU'}`);
    if (commit.output) logs.push(commit.output);
    if (!commit.ok) return { ok: false, logs };
  } else {
    logs.push('Sem mudanças de lojas para commit.');
  }

  const push = await runCommand('git', ['push', 'origin', currentBranch], PROJECT_ROOT);
  logs.push(`git push: ${push.ok ? 'OK' : 'FALHOU'}`);
  if (push.output) logs.push(push.output);
  if (!push.ok) return { ok: false, logs };

  if (!options.VPS_SSH_TARGET) {
    logs.push('VPS_SSH_TARGET não configurado; sincronização remota ignorada.');
    return { ok: true, logs };
  }

  if (!options.VPS_SSH_KEY_PATH) {
    logs.push('VPS_SSH_KEY_PATH não configurado; usando autenticação SSH padrão, se disponível.');
  }

  const sshArgs = [];
  if (options.VPS_SSH_KEY_PATH) {
    sshArgs.push('-i', options.VPS_SSH_KEY_PATH);
  }
  sshArgs.push(options.VPS_SSH_TARGET, `cd ${options.VPS_PROJECT_DIR || '/home/ubuntu/boot-telegram-vendinhas-herosaga'} && git pull --ff-only`);

  const pull = await runCommand('ssh', sshArgs, PROJECT_ROOT);
  logs.push(`ssh git pull: ${pull.ok ? 'OK' : 'FALHOU'}`);
  if (pull.output) logs.push(pull.output);

  if (!pull.ok) {
    logs.push('GitHub já foi atualizado; a VPS pode pegar a mudança no próximo ciclo do worker se ele estiver rodando.');
    return { ok: false, logs };
  }

  if (pull.ok) {
    const scpArgs = [];
    if (options.VPS_SSH_KEY_PATH) {
      scpArgs.push('-i', options.VPS_SSH_KEY_PATH);
    }
    scpArgs.push(ENV_FILE, `${options.VPS_SSH_TARGET}:${options.VPS_PROJECT_DIR || '/home/ubuntu/boot-telegram-vendinhas-herosaga'}/.env`);

    const scp = await runCommand('scp', scpArgs, PROJECT_ROOT);
    logs.push(`scp .env: ${scp.ok ? 'OK' : 'FALHOU'}`);
    if (scp.output) logs.push(scp.output);

    if (scp.ok) {
      const restart = await ensureRemoteWorker(options);
      logs.push(...restart.logs);
      return { ok: restart.ok, logs };
    }
  }

  return { ok: true, logs };
}

async function ensureRemoteWorker(options) {
  if (!options.VPS_SSH_TARGET) {
    return { ok: false, logs: ['VPS_SSH_TARGET não configurado; worker não iniciado.'] };
  }

  const sshArgs = [];
  if (options.VPS_SSH_KEY_PATH) {
    sshArgs.push('-i', options.VPS_SSH_KEY_PATH);
  }
  sshArgs.push(
    options.VPS_SSH_TARGET,
    `cd ${options.VPS_PROJECT_DIR || '/home/ubuntu/boot-telegram-vendinhas-herosaga'} && tmux kill-session -t bot 2>/dev/null || true; LOOP_SECONDS=5 tmux new-session -d -s bot 'bash scripts/run_vps_worker.sh'`
  );

  const restart = await runCommand('ssh', sshArgs, PROJECT_ROOT);
  const logs = [`ssh restart tmux: ${restart.ok ? 'OK' : 'FALHOU'}`];
  if (restart.output) logs.push(restart.output);
  return { ok: restart.ok, logs };
}

async function runBotCheck() {
  const candidates = [
    path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe'),
    'python'
  ];

  let lastResult = null;
  for (const pythonBin of candidates) {
    const result = await runCommand(pythonBin, ['bot.py'], PROJECT_ROOT, {
      env: {
        TELEGRAM_SMOKE_TEST: '1'
      }
    });
    lastResult = result;
    if (result.ok || pythonBin === 'python') {
      break;
    }
  }

  return lastResult ?? { ok: false, output: 'Falha ao executar bot.py', code: 1 };
}

async function runMonitorCycle(options) {
  const candidates = [
    path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe'),
    'python'
  ];

  let lastResult = null;
  const runtimeEnv = buildPythonEnv(options.env || {});

  for (const pythonBin of candidates) {
    const result = await runCommand(pythonBin, ['bot.py'], PROJECT_ROOT, {
      env: runtimeEnv
    });
    lastResult = result;
    if (result.ok || pythonBin === 'python') {
      break;
    }
  }

  return lastResult ?? { ok: false, output: 'Falha ao executar bot.py', code: 1 };
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

ipcMain.handle('config:load', async () => {
  const env = loadEnv();
  return {
    shops: loadShops(),
    env: {
      TOKEN: env.TOKEN || env.BOT_TOKEN || '',
      CHAT_ID: env.CHAT_ID || env.TELEGRAM_CHAT_ID || '',
      CHAT_IDS: env.CHAT_IDS || '',
      TELEGRAM_MESSAGE: env.TELEGRAM_MESSAGE || '',
      DISCORD_WEBHOOK: env.DISCORD_WEBHOOK || '',
        DISCORD_MESSAGE: env.DISCORD_MESSAGE || '',
      VPS_SSH_TARGET: env.VPS_SSH_TARGET || 'ubuntu@147.15.31.133',
      VPS_SSH_KEY_PATH: env.VPS_SSH_KEY_PATH || '',
      VPS_PROJECT_DIR: env.VPS_PROJECT_DIR || '/home/ubuntu/boot-telegram-vendinhas-herosaga'
    }
  };
});

ipcMain.handle('config:save', async (_event, payload) => {
  const shops = (payload.shops || []).map(normalizeShop).filter((shop) => shop.url);
  saveShops(shops);
  const savedEnv = saveEnv(payload.env || {});
  return buildSaveResponse(shops, savedEnv, [
    `Lojinhas salvas: ${shops.length}`,
    'Arquivo gerado: config/shop_urls.txt',
    'Arquivo gerado: .env'
  ]);
});

ipcMain.handle('ops:sync', async (_event, payload) => {
  const shops = (payload.shops || []).map(normalizeShop).filter((shop) => shop.url);
  saveShops(shops);
  const env = saveEnv(payload.env || {});
  return syncToVps(env);
});

ipcMain.handle('ops:runBotCheck', async () => runBotCheck());

ipcMain.handle('ops:runMonitorCycle', async (_event, payload) => runMonitorCycle(payload || {}));

ipcMain.handle('ops:ensureWorker', async (_event, payload) => {
  const env = saveEnv(payload.env || {});
  return ensureRemoteWorker(env);
});

app.whenReady().then(() => {
  ensureFiles();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
