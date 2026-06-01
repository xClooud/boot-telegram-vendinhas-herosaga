#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOOP_SECONDS="${LOOP_SECONDS:-5}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

if ! [[ "$LOOP_SECONDS" =~ ^[0-9]+$ ]] || [ "$LOOP_SECONDS" -lt 3 ]; then
  LOOP_SECONDS=5
fi

cd "$ROOT_DIR"

unset TELEGRAM_SMOKE_TEST

if [ -f .env ]; then
  sed -i '/^TELEGRAM_SMOKE_TEST=/d' .env
fi

while true; do
  CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
  if [ -z "$CURRENT_BRANCH" ]; then
    log "nao foi possivel detectar o branch atual; tentando novamente no proximo ciclo"
    sleep "$LOOP_SECONDS"
    continue
  fi

  HISTORY_BACKUP="$(mktemp)"
  HISTORY_PRESENT=0
  if [ -f data/history.json ]; then
    cp data/history.json "$HISTORY_BACKUP"
    HISTORY_PRESENT=1
  fi

  rm -f data/history.json
  if ! git pull --ff-only origin "$CURRENT_BRANCH"; then
    log "git pull falhou; restaurando historico e tentando novamente no proximo ciclo"
    if [ "$HISTORY_PRESENT" -eq 1 ] && [ -f "$HISTORY_BACKUP" ]; then
      mv "$HISTORY_BACKUP" data/history.json
    else
      rm -f "$HISTORY_BACKUP"
    fi
    sleep "$LOOP_SECONDS"
    continue
  fi

  if [ "$HISTORY_PRESENT" -eq 1 ] && [ -f "$HISTORY_BACKUP" ]; then
    mv "$HISTORY_BACKUP" data/history.json
  else
    rm -f "$HISTORY_BACKUP"
  fi

  if [ ! -f .venv/bin/activate ]; then
    log "virtualenv nao encontrada em .venv/bin/activate; tentando novamente no proximo ciclo"
    sleep "$LOOP_SECONDS"
    continue
  fi

  . .venv/bin/activate
  if ! python3 bot.py; then
    log "execucao do bot falhou; mantendo loop ativo para nova tentativa"
  fi

  sleep "$LOOP_SECONDS"
done
