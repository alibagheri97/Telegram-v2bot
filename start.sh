#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

umask 022
PREPARE_ONLY=false
if [[ "${1:-}" == "--prepare-only" ]]; then
  PREPARE_ONLY=true
  shift
fi

SELECTED_PYTHON=""
for candidate in "${PYTHON_BIN:-}" python3.14 python3.13 python3.12 python3.11 python3; do
  [[ -n "$candidate" ]] || continue
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    SELECTED_PYTHON="$candidate"
    break
  fi
done
[[ -n "$SELECTED_PYTHON" ]] || {
  echo "Python 3.11+ is required. Run: bash install-prerequisites.sh" >&2
  exit 1
}

for required_command in systemctl git; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "$required_command is required for service installation." >&2
    exit 1
  }
done
if [[ "${EUID}" -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "sudo is required for a non-root install." >&2; exit 1; }
else
  echo "Root setup is supported; the bot service will use a dedicated unprivileged account."
fi

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  if [[ -e .venv || -L .venv ]]; then
    backup_path="$PROJECT_DIR/.venv.incompatible-$(date +%s)"
    [[ ! -e "$backup_path" ]] || { echo "Virtualenv backup already exists; inspect it first." >&2; exit 1; }
    mv -- "$PROJECT_DIR/.venv" "$backup_path"
    echo "Preserved the old virtualenv at: $backup_path"
  fi
  "$SELECTED_PYTHON" -m venv .venv || {
    echo "venv creation failed. Run: bash install-prerequisites.sh" >&2
    exit 1
  }
fi

.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
.venv/bin/python -m pip install --disable-pip-version-check --no-deps -e .
if [[ "$PREPARE_ONLY" == true ]]; then
  .venv/bin/python -c 'import telebot, pydantic_settings, sqlalchemy, pymysql, bcrypt, apscheduler, PIL, flask, gunicorn; print("Dependency imports OK")'
  echo "Dependencies are ready. No bot, database setup or service was started."
  echo "Run ./start.sh when you are ready for interactive installation."
  exit 0
fi
exec .venv/bin/python -m vpnshopbot.setup "$@"
