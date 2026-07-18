#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"

if lsof -iTCP:8483 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "Backend already listening on http://127.0.0.1:8483"
else
  (
    cd "$ROOT_DIR/backend"
    export OPENAI_MAX_REQUEST_BYTES="${OPENAI_MAX_REQUEST_BYTES:-18000}"
    export OPENAI_MAX_MERGE_REQUEST_BYTES="${OPENAI_MAX_MERGE_REQUEST_BYTES:-64000}"
    export OPENAI_MIN_REQUEST_BYTES="${OPENAI_MIN_REQUEST_BYTES:-6000}"
    export OPENAI_CHUNK_SHRINK_FACTOR="${OPENAI_CHUNK_SHRINK_FACTOR:-0.5}"
    export OPENAI_RETRY_ATTEMPTS="${OPENAI_RETRY_ATTEMPTS:-5}"
    export OPENAI_RETRY_BACKOFF_SECONDS="${OPENAI_RETRY_BACKOFF_SECONDS:-2}"
    export OPENAI_RETRY_JITTER_RATIO="${OPENAI_RETRY_JITTER_RATIO:-0.25}"
    export OPENAI_SDK_MAX_RETRIES="${OPENAI_SDK_MAX_RETRIES:-0}"
    export OPENAI_STREAM_RESPONSES="${OPENAI_STREAM_RESPONSES:-1}"
    nohup "$ROOT_DIR/.venv/bin/python" main.py > "$LOG_DIR/backend.log" 2>&1 &
    echo $! > "$LOG_DIR/backend.pid"
  )
  echo "Started backend, log: $LOG_DIR/backend.log"
fi

if lsof -iTCP:3015 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "Frontend already listening on http://127.0.0.1:3015"
else
  (
    cd "$ROOT_DIR/BillNote_frontend"
    npm_config_cache="$ROOT_DIR/.npm-cache" nohup npx -y pnpm@9.15.0 dev --host 0.0.0.0 > "$LOG_DIR/frontend.log" 2>&1 &
    echo $! > "$LOG_DIR/frontend.pid"
  )
  echo "Started frontend, log: $LOG_DIR/frontend.log"
fi

APP_URL="http://127.0.0.1:3015/"

for _ in {1..60}; do
  if curl -fsS "$APP_URL" >/dev/null 2>&1; then
    open "$APP_URL"
    echo "Opened $APP_URL"
    exit 0
  fi
  sleep 1
done

echo "Open $APP_URL"
