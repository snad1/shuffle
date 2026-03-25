# Changelog

## v1.0.0 — Initial Release (2026-03-25)

### CLI Bot v1.0.0

#### Added

- **Multi-game support** — Limbo + Dice via pluggable game registry
- **GraphQL API** — Single endpoint at `shuffle.com/main-api/graphql/api/graphql` with Bearer token auth
- **7 betting strategies** — Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Delay Martingale, Rule-Based
- **Rule-based engine** — Build custom IF/THEN rules with interactive wizard
- **Named presets** — Save & load strategy configs by name
- **Pure ANSI TUI** — Direct ANSI rendering, fits any terminal, refreshes 2x/sec
- **SQLite session + bet logging** — Full history survives restarts
- **Daemon mode** — `--daemon`, `--resume`, `--status`, `--stop`, `--monitor`
- **Smart stop conditions** — Max profit, max loss, max bets, max wins, min balance
- **20% balance safety cap** — Never bets more than 20% of current balance
- **curl_cffi Chrome TLS** — Bypasses TLS fingerprinting on servers with retry-on-reset
- **Daily log rotation** — 30-day retention

### TG Bot v1.0.0

#### Added

- **Telegram Bot** (`shuffle/tg/`) — Full multi-tenant Telegram bot for Shuffle auto-betting
- **Multi-game support** — Limbo + Dice
- **Multi-session support** — Up to 5 concurrent betting sessions per user
- **22 commands** — `/settoken`, `/balance`, `/config`, `/set`, `/strategies`, `/bet`, `/stop`, `/pause`, `/resume`, `/status`, `/monitor`, `/stats`, `/session`, `/lastbets`, `/rules`, `/addrule`, `/clearrules`, `/presets`, `/savepreset`, `/loadpreset`, `/help`, `/start`
- **Live monitor** — Auto-updating status messages with inline buttons
- **Shared core module** (`shuffle/core/`) — Strategy, database, and engine logic shared between CLI and TG bot
- **Bearer token auth** — Single token, no CloudFlare bypass needed
- **curl_cffi Chrome TLS** — Handles server TLS fingerprinting with retry-on-reset
- **Batched DB writes** — Flush every 50 bets via `executemany()`
- **Periodic session save** — Stats saved every 30s
- **Auto data retention** — `/cleanup` purges old bet records
- **Preset security** — Presets exclude bearer_token

### Web Admin v0.1.0

#### Added

- **Admin dashboard** — Platform overview, system metrics, service management
- **User management** — View TG users, sessions, bets, tier management
- **Session detail** — Balance chart (TradingView Lightweight Charts), bet history with CSV export
- **Live features** — WebSocket metrics, log streaming, live session charts
- **Audit log** — Track all admin actions
- **Auth** — JWT-based admin login + Telegram user deep-link auth
- **Dark mode** — Toggle with localStorage persistence

### Infrastructure

- **`shufflectl`** — Server management CLI (setup, start, stop, monitor, update, TG/web management)
- **`install.sh`** — Automated server installer (venv, systemd, env config)
- **Web installer** — `web/install.sh` with Nginx + SSL support
