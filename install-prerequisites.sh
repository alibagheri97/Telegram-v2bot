#!/usr/bin/env bash
# System prerequisites only. Never starts the bot or changes the panel database.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip git ca-certificates sudo
  if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    apt-get install -y python3.11 python3.11-venv || {
      echo "This OS repository does not provide Python 3.11. Install Python 3.11+ from an approved source, then set PYTHON_BIN." >&2
      exit 1
    }
  fi
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3.11 python3.11-pip git ca-certificates sudo
else
  echo "Unsupported package manager. Install Python 3.11+, venv/pip, git and systemd manually." >&2
  exit 1
fi
echo "System prerequisites installed. Existing MariaDB/panel services were not reconfigured."
echo "Next: bash start.sh --prepare-only"
