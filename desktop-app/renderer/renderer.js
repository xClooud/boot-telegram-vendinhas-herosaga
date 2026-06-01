const shopsList = document.getElementById('shops-list');
const statusPill = document.getElementById('status-pill');
const summaryText = document.getElementById('summary-text');
const logsOutput = document.getElementById('logs-output');
const shopsCountEl = document.getElementById('shops-count');
const telegramStateEl = document.getElementById('telegram-state');
const discordStateEl = document.getElementById('discord-state');
const vpsStateEl = document.getElementById('vps-state');
const template = document.getElementById('shop-row-template');
const pageButtons = [...document.querySelectorAll('.page-tab')];
const pagePanels = [...document.querySelectorAll('.page-panel')];

const shopCurrencyOptions = [
  { value: '', label: 'Automático' },
  { value: 'Zeny', label: 'Zeny' },
  { value: 'Hero Points', label: 'Hero Points' },
  { value: 'Moeda RMT', label: 'Moeda RMT' }
];

const DEFAULT_TELEGRAM_MESSAGE = '🛒 VENDA DETECTADA\nLoja: {store}\nMoeda: {currency}\nItem: {item}\nQuantidade: {quantity}\nQuantidade comprada: {bought_quantity}\nPreço: {price}\nHora: {time}\n\n{img_url}';
const DEFAULT_DISCORD_MESSAGE = '[ALERTA HERO] {default}';

const envFields = [
  'TOKEN',
  'CHAT_ID',
  'CHAT_IDS',
  'TELEGRAM_MESSAGE',
  'NOTIFY_COOLDOWN',
  'REQUEST_TIMEOUT',
  'DISCORD_WEBHOOK',
  'DISCORD_MESSAGE',
  'VPS_SSH_TARGET',
  'VPS_SSH_KEY_PATH',
  'VPS_PROJECT_DIR'
];

const buttons = [
  document.getElementById('save-btn'),
  document.getElementById('save-only-btn'),
  document.getElementById('sync-only-btn'),
  document.getElementById('ensure-worker-btn'),
  document.getElementById('run-bot-btn'),
  document.getElementById('add-shop-btn'),
  document.getElementById('restore-messages-btn')
];

let autoSaveMessageTimer = null;
let isLoadingConfig = false;

function setBusy(isBusy) {
  for (const button of buttons) {
    if (button) {
      button.disabled = isBusy;
      button.style.opacity = isBusy ? '0.72' : '1';
      button.style.cursor = isBusy ? 'wait' : 'pointer';
    }
  }

  for (const button of document.querySelectorAll('.save-shop-btn, .remove-shop-btn')) {
    button.disabled = isBusy;
    button.style.opacity = isBusy ? '0.72' : '1';
    button.style.cursor = isBusy ? 'wait' : 'pointer';
  }
}

function setStatus(text, tone = 'info') {
  const statusText = statusPill.querySelector('.status-text');
  if (statusText) {
    statusText.textContent = text;
  } else {
    statusPill.textContent = text;
  }
  statusPill.dataset.tone = tone;
}

function setLogs(text) {
  logsOutput.textContent = text || '';
}

function appendLogs(lines) {
  setLogs(Array.isArray(lines) ? lines.join('\n') : String(lines || ''));
}

function restoreDefaultMessages() {
  const telegramMessage = document.getElementById('TELEGRAM_MESSAGE');
  const discordMessage = document.getElementById('DISCORD_MESSAGE');

  if (telegramMessage) {
    telegramMessage.value = DEFAULT_TELEGRAM_MESSAGE;
  }

  if (discordMessage) {
    discordMessage.value = DEFAULT_DISCORD_MESSAGE;
  }

  scheduleAutoSaveMessageTemplates();
}

function scheduleAutoSaveMessageTemplates() {
  if (isLoadingConfig) {
    return;
  }

  if (autoSaveMessageTimer) {
    clearTimeout(autoSaveMessageTimer);
  }

  autoSaveMessageTimer = setTimeout(async () => {
    autoSaveMessageTimer = null;

    try {
      await window.heroDesktop.saveConfig({ shops: collectShops(), env: collectEnv() });
      setStatus('Mensagens salvas automaticamente', 'success');
    } catch (error) {
      setStatus(`Falha ao salvar mensagens: ${error.message}`, 'error');
      setLogs(error.stack || error.message);
    }
  }, 800);
}

function setRowSavedState(row, saved, label = '') {
  if (!row) {
    return;
  }

  row.dataset.saved = saved ? 'true' : 'false';
  const stateLabel = row.querySelector('.shop-save-state');
  if (stateLabel) {
    stateLabel.textContent = label || (saved ? 'Lojinha salva' : 'Lojinha alterada');
  }
}

function hasVpsConfig(env = collectEnv()) {
  return Boolean(env.VPS_SSH_TARGET && env.VPS_PROJECT_DIR);
}

async function syncShopsToVps(shops, env) {
  const result = await window.heroDesktop.syncToVps({ shops, env });
  appendLogs(result.logs || ['Sincronização sem saída.']);
  return result;
}

async function saveAllShops(options = {}) {
  const { message = 'Configuração salva.', syncRemote = false } = options;
  const shops = collectShops();
  const err = validateShops(shops);
  if (err) {
    throw new Error(err);
  }

  const env = collectEnv();
  const result = await window.heroDesktop.saveConfig({ shops, env });

  for (const row of document.querySelectorAll('.shop-row')) {
    setRowSavedState(row, true);
  }
  appendLogs(result.logs || [message]);
  appendLogs(['As lojas novas entram em baseline no próximo ciclo; a primeira leitura só grava o estado atual.']);

  if (!syncRemote) {
    return result;
  }

  if (!hasVpsConfig(env)) {
    appendLogs(['VPS não configurada; salvamento local concluído.']);
    return result;
  }

  setStatus('Sincronizando VPS...', 'info');
  const syncResult = await syncShopsToVps(shops, env);
  setStatus(syncResult.ok ? 'Sincronizado e ligado' : 'Desligado', syncResult.ok ? 'success' : 'error');
  return syncResult;
}

function setActivePage(pageName) {
  for (const button of pageButtons) {
    button.classList.toggle('active', button.dataset.page === pageName);
  }

  for (const panel of pagePanels) {
    panel.classList.toggle('active', panel.dataset.pagePanel === pageName);
  }

  const target = document.querySelector(`[data-page-panel="${pageName}"]`);
  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function addShopRow(shop = { name: '', url: '', currency: '' }) {
  const fragment = template.content.cloneNode(true);
  const row = fragment.querySelector('.shop-row');
  const nameInput = fragment.querySelector('.shop-name');
  const urlInput = fragment.querySelector('.shop-url');
  const currencySelect = fragment.querySelector('.shop-currency');
  const saveBtn = fragment.querySelector('.save-shop-btn');
  const removeBtn = fragment.querySelector('.remove-shop-btn');

  nameInput.value = shop.name || '';
  urlInput.value = shop.url || '';
  if (currencySelect) {
    currencySelect.value = shop.currency || '';
  }
  setRowSavedState(row, Boolean((shop.name || '').trim() || (shop.url || '').trim()), shop.url ? 'Lojinha salva' : 'Lojinha não salva');

  const refresh = () => {
    setRowSavedState(row, false);
    updateSummary();
  };
  nameInput.addEventListener('input', refresh);
  urlInput.addEventListener('input', refresh);
  if (currencySelect) {
    currencySelect.addEventListener('change', refresh);
  }

  saveBtn.addEventListener('click', async () => {
    const err = validateShops(collectShops());
    if (err) {
      setStatus(err, 'error');
      setLogs(err);
      return;
    }

    setBusy(true);
    setStatus('Salvando lojinha...', 'info');
    try {
      const result = await saveAllShops({ message: 'Lojinha salva com sucesso.', syncRemote: true });
      setRowSavedState(row, true, 'Lojinha salva');
      appendLogs(result.logs || ['Lojinha salva com sucesso.']);
      setStatus('Lojinha salva e sincronizada', 'success');
      updateSummary();
    } catch (error) {
      setStatus(`Falha ao salvar lojinha: ${error.message}`, 'error');
      setLogs(error.stack || error.message);
    } finally {
      setBusy(false);
    }
  });

  removeBtn.addEventListener('click', () => {
    row.remove();
    if (shopsList.children.length === 0) {
      addShopRow();
    }
    updateSummary();
  });

  shopsList.appendChild(fragment);
}

function collectShops() {
  return [...shopsList.querySelectorAll('.shop-row')]
    .map((row) => ({
      name: row.querySelector('.shop-name')?.value?.trim() || '',
      url: row.querySelector('.shop-url')?.value?.trim() || '',
      currency: row.querySelector('.shop-currency')?.value?.trim() || ''
    }))
    .filter((shop) => shop.url);
}

function collectEnv() {
  const env = {};
  for (const field of envFields) {
    const element = document.getElementById(field);
    env[field] = element ? element.value.trim() : '';
  }
  return env;
}

function fillEnv(env) {
  for (const field of envFields) {
    const element = document.getElementById(field);
    if (element) {
      element.value = env[field] || '';
    }
  }
}

function updateSummary() {
  const shops = collectShops();
  const env = collectEnv();
  const telegramReady = Boolean((env.TOKEN || '') && ((env.CHAT_ID || '') || (env.CHAT_IDS || '')));
  const discordReady = Boolean(env.DISCORD_WEBHOOK);
  const vpsReady = Boolean(env.VPS_SSH_TARGET && env.VPS_PROJECT_DIR);

  shopsCountEl.textContent = String(shops.length);
  telegramStateEl.textContent = telegramReady ? 'Ligado' : 'Desligado';
  discordStateEl.textContent = discordReady ? 'Ligado' : 'Desligado';
  vpsStateEl.textContent = vpsReady ? 'Ligado' : 'Desligado';
  telegramStateEl.dataset.state = telegramReady ? 'on' : 'off';
  discordStateEl.dataset.state = discordReady ? 'on' : 'off';
  vpsStateEl.dataset.state = vpsReady ? 'on' : 'off';

  summaryText.textContent = shops.length
    ? `${shops.length} lojinha(s) configurada(s). Telegram ${telegramReady ? 'ligado' : 'desligado'}, Discord ${discordReady ? 'ligado' : 'desligado'}, VPS ${vpsReady ? 'ligada' : 'desligada'}.`
    : 'Adicione sua primeira lojinha para começar.';
}

function validateShops(shops) {
  if (shops.length === 0) {
    return 'Cadastre pelo menos uma lojinha com URL.';
  }
  for (const shop of shops) {
    if (!shop.url.startsWith('http://') && !shop.url.startsWith('https://')) {
      return `URL inválida: ${shop.url}`;
    }
  }
  return '';
}

async function load() {
  isLoadingConfig = true;
  setBusy(true);
  setStatus('Conectando...', 'info');
  try {
    const payload = await window.heroDesktop.loadConfig();
    shopsList.innerHTML = '';
    const shops = payload.shops && payload.shops.length ? payload.shops : [{ name: '', url: '' }];
    shops.forEach(addShopRow);
    fillEnv(payload.env || {});
    for (const row of document.querySelectorAll('.shop-row')) {
      setRowSavedState(row, true, 'Lojinha salva');
    }
    updateSummary();
    setLogs('Configuração carregada.');
    setStatus('Ligado', 'success');
  } catch (error) {
    setStatus(`Erro ao carregar: ${error.message}`, 'error');
    setLogs(error.stack || error.message);
  } finally {
    isLoadingConfig = false;
    setBusy(false);
  }
}

async function saveOnly() {
  setBusy(true);
  setStatus('Salvando...', 'info');
  try {
    const result = await saveAllShops({ message: 'Configuração salva.' });
    setStatus('Salvo e ligado', 'success');
    updateSummary();
  } catch (error) {
    setStatus(`Falha ao salvar: ${error.message}`, 'error');
    setLogs(error.stack || error.message);
  } finally {
    setBusy(false);
  }
}

async function saveShopsAndSync() {
  setBusy(true);
  setStatus('Salvando e sincronizando...', 'info');
  setLogs('Atualizando lojas locais e enviando a versão nova para a VPS...');
  try {
    const result = await saveAllShops({ message: 'Configuração salva.', syncRemote: true });
    appendLogs(result.logs || ['Sincronização concluída.']);
    setStatus(result.ok ? 'Lojinhas sincronizadas' : 'Sincronização falhou', result.ok ? 'success' : 'error');
    updateSummary();
  } catch (error) {
    setStatus(`Falha ao sincronizar lojinhas: ${error.message}`, 'error');
    setLogs(error.stack || error.message);
  } finally {
    setBusy(false);
  }
}

async function syncNow() {
  setBusy(true);
  setStatus('Sincronizando...', 'info');
  setLogs('Preparando envio para GitHub e VPS...');
  try {
    const shops = collectShops();
    const err = validateShops(shops);
    if (err) {
      throw new Error(err);
    }

    const result = await window.heroDesktop.syncToVps({ shops, env: collectEnv() });
    appendLogs(result.logs || ['Sincronização sem saída.']);
    setStatus(result.ok ? 'Sincronizado e ligado' : 'Desligado', result.ok ? 'success' : 'error');
  } catch (error) {
    setStatus(`Erro na sincronização: ${error.message}`, 'error');
    setLogs(error.stack || error.message);
  } finally {
    setBusy(false);
    updateSummary();
  }
}

async function ensureWorker() {
  setBusy(true);
  setStatus('Restaurando worker...', 'info');
  setLogs('Tentando recriar a sessão tmux bot...');
  try {
    const result = await window.heroDesktop.ensureWorker({ env: collectEnv() });
    appendLogs(result.logs || ['Worker restaurado.']);
    setStatus(result.ok ? 'Worker ligado na VPS' : 'Worker desligado', result.ok ? 'success' : 'error');
  } catch (error) {
    setStatus(`Erro ao restaurar worker: ${error.message}`, 'error');
    setLogs(error.stack || error.message);
  } finally {
    setBusy(false);
  }
}

async function runBotCheck() {
  setBusy(true);
  setStatus('Atualizando status...', 'info');
  setLogs('Executando um ciclo local do monitor para atualizar os status das lojas...');
  try {
    const result = await window.heroDesktop.runMonitorCycle({ env: collectEnv() });
    setLogs(result.output || '(sem saída)');
    setStatus(result.ok ? 'Status atualizado' : 'Falha ao atualizar', result.ok ? 'success' : 'error');
  } catch (error) {
    setStatus(`Erro ao atualizar status: ${error.message}`, 'error');
    setLogs(error.stack || error.message);
  } finally {
    setBusy(false);
  }
}

document.getElementById('add-shop-btn').addEventListener('click', () => addShopRow());
document.getElementById('save-btn').addEventListener('click', saveShopsAndSync);
document.getElementById('save-only-btn').addEventListener('click', saveOnly);
document.getElementById('sync-only-btn').addEventListener('click', syncNow);
document.getElementById('ensure-worker-btn').addEventListener('click', ensureWorker);
document.getElementById('run-bot-btn').addEventListener('click', runBotCheck);
const restoreMessagesBtn = document.getElementById('restore-messages-btn');
if (restoreMessagesBtn) {
  restoreMessagesBtn.addEventListener('click', restoreDefaultMessages);
}

for (const button of pageButtons) {
  button.addEventListener('click', () => setActivePage(button.dataset.page));
}

for (const field of envFields) {
  const element = document.getElementById(field);
  if (element) {
    element.addEventListener('input', updateSummary);
  }
}

for (const field of ['TELEGRAM_MESSAGE', 'DISCORD_MESSAGE']) {
  const element = document.getElementById(field);
  if (element) {
    element.addEventListener('input', scheduleAutoSaveMessageTemplates);
  }
}

setActivePage('lojinhas');
load();
