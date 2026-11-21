# Changelog

## 1.0.1

- Root-friendly server setup with an unprivileged service account and private runtime state.
- Python 3.11+ interpreter discovery, recoverable virtualenv replacement and preparation-only mode.
- Separate OS prerequisite installer; no bot, panel or database is started during preparation.
- Root-aware systemd commands and backwards-compatible reading of v1 health reports.

## v1 / 1.0.0

- Persian TeleBot VPN shop with separate bot/panel databases and one-time root setup.
- Customer payments, wallets, referrals, QR guides and reseller-specific pricing.
- Shared driving lists, root user controls, audit history and per-admin wallet credit.
- Expiry notices, emergency advances, debt-aware monthly renewal and reversible archives.
- Staged release bundles and patches, preflight checks and same-PID graceful updates.

Configuration and secrets are local-only. Database rollback is not performed by code rollback.
