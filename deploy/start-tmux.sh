#!/usr/bin/env bash
set -euo pipefail

# Start split-extract API in tmux session "splitextract".
# Usage: bash start-tmux.sh

APP_DIR="/var/www/html/split-extract"
SESSION_NAME="splitextract"
PORT="${PORT:-8001}"
LOG_FILE="${APP_DIR}/logs/service.log"
UVICORN_CMD="/var/www/html/venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 1200"

mkdir -p "${APP_DIR}/logs"

# Stop existing tmux session and any orphaned uvicorn on this port.
tmux kill-session -t "${SESSION_NAME}" 2>/dev/null || true
sleep 1
if command -v ss >/dev/null 2>&1; then
  OLD_PID="$(ss -ltnp 2>/dev/null | grep ":${PORT} " | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
  if [ -n "${OLD_PID}" ]; then
    kill "${OLD_PID}" 2>/dev/null || true
    sleep 1
  fi
fi

tmux set-option -g history-limit 100000
tmux new-session -d -s "${SESSION_NAME}" -c "${APP_DIR}" "${UVICORN_CMD}"
tmux set-option -t "${SESSION_NAME}" history-limit 100000
tmux pipe-pane -t "${SESSION_NAME}" -o "cat >> ${LOG_FILE}"

sleep 5
if curl -sf --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "split-extract running in tmux session '${SESSION_NAME}' on port ${PORT}"
  echo "  Attach:  tmux attach -t ${SESSION_NAME}"
  echo "  Logs:    tail -f ${LOG_FILE}"
else
  echo "Health check failed. Recent tmux output:"
  tmux capture-pane -t "${SESSION_NAME}" -p | tail -20
  exit 1
fi
