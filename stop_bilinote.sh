#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT_DIR/logs"

stop_pid_file() {
  local pid_file="$1"
  local name="$2"

  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file")"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      echo "Stopped $name pid $pid"
    fi
    rm -f "$pid_file"
  fi
}

stop_pid_file "$LOG_DIR/backend.pid" "backend"
stop_pid_file "$LOG_DIR/frontend.pid" "frontend"

pkill -f "$ROOT_DIR/backend/main.py" >/dev/null 2>&1 || true
pkill -f "$ROOT_DIR/.npm-cache/.*/pnpm dev --host 0.0.0.0" >/dev/null 2>&1 || true
pkill -f "$ROOT_DIR/BillNote_frontend/node_modules/.bin/vite" >/dev/null 2>&1 || true

echo "BiliNote stopped."
