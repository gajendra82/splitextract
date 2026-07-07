#!/usr/bin/env bash
set -euo pipefail

# Deploy split-extract API on a tmux server via git clone + Docker.
# Usage: bash deploy-tmux.sh

APP_NAME="split-extract"
SESSION_NAME="split-extract"
REPO_URL="https://github.com/gajendra82/splitextract.git"
INSTALL_DIR="${HOME}/${APP_NAME}"
PORT="${PORT:-8001}"

echo "==> Installing to ${INSTALL_DIR}"

if [ -d "${INSTALL_DIR}/.git" ]; then
  echo "==> Repo exists, pulling latest..."
  git -C "${INSTALL_DIR}" pull origin main
else
  echo "==> Cloning repo..."
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
  echo "!! Edit ${INSTALL_DIR}/.env with production keys before starting."
  exit 1
fi

mkdir -p logs

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker first."
  exit 1
fi

echo "==> Building and starting container..."
docker compose down 2>/dev/null || true
docker compose up -d --build

echo "==> Waiting for health check..."
sleep 5
curl -sf "http://127.0.0.1:${PORT}/health" | python3 -m json.tool || {
  echo "Health check failed. Logs:"
  docker compose logs --tail=50
  exit 1
}

if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t "${SESSION_NAME}" 2>/dev/null || true
  tmux new-session -d -s "${SESSION_NAME}" "cd ${INSTALL_DIR} && docker compose logs -f"
  echo "==> tmux session '${SESSION_NAME}' attached to container logs"
  echo "    Attach: tmux attach -t ${SESSION_NAME}"
else
  echo "tmux not installed; container runs in background via docker compose."
fi

echo "==> Deploy complete"
echo "    API: http://127.0.0.1:${PORT}"
echo "    Health: http://127.0.0.1:${PORT}/health"
echo "    Docs: http://127.0.0.1:${PORT}/docs"
