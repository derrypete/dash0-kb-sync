#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Load optional environment variables (e.g. GITHUB_TOKEN) if configured
if [[ -f "$PROJECT_DIR/config/.env" ]]; then
  set -a
  source "$PROJECT_DIR/config/.env"
  set +a
elif [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
fi

# Run --force-full every Sunday (day 7 in ISO-8601 `date +%u`), otherwise --incremental
if [[ "$(date +%u)" == "7" ]]; then
  SYNC_MODE="--force-full"
else
  SYNC_MODE="--incremental"
fi

echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Starting dash0-kb-sync ($SYNC_MODE)..."

PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
if [[ -x "$PYTHON_BIN" ]]; then
  exec "$PYTHON_BIN" -m dash0_kb_sync run "$SYNC_MODE"
elif command -v uv >/dev/null 2>&1; then
  exec uv run python -m dash0_kb_sync run "$SYNC_MODE"
else
  echo "Error: Virtualenv not found at $PYTHON_BIN and uv is not in PATH." >&2
  exit 1
fi
