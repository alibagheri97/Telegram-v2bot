#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export VPN_SHOP_ENV_FILE="$PROJECT_DIR/.env"
export VPN_SHOP_RUNTIME_DIR="$PROJECT_DIR/.runtime"
RELEASE_DIR="$(readlink -f "$VPN_SHOP_RUNTIME_DIR/current")"
[[ "$RELEASE_DIR" == "$VPN_SHOP_RUNTIME_DIR/releases/"* ]] || {
  echo "No valid managed release; run start.sh first." >&2
  exit 1
}
cd "$RELEASE_DIR/src"
export VPN_SHOP_RELEASE_ID="$(basename "$RELEASE_DIR")"
exec "$RELEASE_DIR/venv/bin/python" -m vpnshopbot.app
