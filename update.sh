#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$PROJECT_DIR/.runtime/current/venv/bin/python" ]]; then
  cd "$PROJECT_DIR/.runtime/current/src"
  exec "$PROJECT_DIR/.runtime/current/venv/bin/python" -m vpnshopbot.release --project "$PROJECT_DIR" update "$@"
fi
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" -m vpnshopbot.release --project "$PROJECT_DIR" update "$@"
