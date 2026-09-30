#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
RUN_DIR="$ROOT_DIR/.run"
BACKEND_PORT=8000
FRONTEND_PORT=3000

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少命令：$1" >&2
    exit 1
  fi
}

ensure_port_available() {
  if lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "端口 $1 已被占用，请先停止占用该端口的服务。" >&2
    exit 1
  fi
}

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "未找到 $PYTHON_BIN。请先按 README.md 完成 Python 依赖安装。" >&2
  exit 1
fi

require_command npm
require_command lsof
ensure_port_available "$BACKEND_PORT"
ensure_port_available "$FRONTEND_PORT"

mkdir -p "$RUN_DIR"
(
  cd "$ROOT_DIR/backend"
  "$PYTHON_BIN" -c 'from app.database import ensure_database_exists; ensure_database_exists()'
)
"$PYTHON_BIN" -m alembic -c "$ROOT_DIR/alembic.ini" upgrade head

(
  cd "$ROOT_DIR/backend"
  exec "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
) >"$RUN_DIR/backend.log" 2>&1 &
BACKEND_PID=$!

(
  cd "$ROOT_DIR/frontend"
  exec npm run dev -- --hostname 127.0.0.1 --port "$FRONTEND_PORT"
) >"$RUN_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!

cleanup() {
  trap - EXIT INT TERM
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
  wait "$FRONTEND_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "后端：http://127.0.0.1:$BACKEND_PORT"
echo "前端：http://127.0.0.1:$FRONTEND_PORT"
echo "按 Ctrl+C 停止服务；日志目录：$RUN_DIR"

wait "$BACKEND_PID"
