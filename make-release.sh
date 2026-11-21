#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d '\r\n' < "$PROJECT_DIR/VERSION")"
exec "$PROJECT_DIR/.venv/bin/python" -m vpnshopbot.release --project "$PROJECT_DIR" bundle --output "$PROJECT_DIR/dist/vpn-shop-v${VERSION}.tar.gz"
