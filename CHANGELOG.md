# Changelog

## v1.0.2 — Win Detection, Security & TG Fixes (2026-03-29)

### CLI Bot v1.0.2

#### Fixed

- **Win detection on free/zero bets** — `is_win = payout > 0` always returned false on zero-amount bets (payout is 0 even on wins). Limbo now checks `result_multiplier >= target_multiplier`, Dice checks `result > target` for ABOVE or `result < target` for BELOW
- **Connection test** — Replaced zero-bet test with read-only `GetMyProfile` query after Shuffle rejected zero bets with `ZERO_BET_LIMIT`

#### Security

- **ENV-controlled error messages** — TG bot error handlers now log full details server-side and show user-friendly messages in production. Controlled via `APP_ENV` environment variable (defaults to `production`)
- **`shufflectl` self-update safety** — Script wrapped in `{ }` block so bash reads entire file into memory before executing, preventing parse errors when `cmd_update` replaces the file mid-run

### TG Bot v1.0.2

#### Fixed

- **Win detection on free/zero bets** — Same result-based fix applied to TG engine for both Limbo and Dice
- **Connection test** — Replaced zero-bet test with profile query (same as CLI)
- **GraphQL schema** — Removed deprecated `AuthenticatedUser` type from balance query (Shuffle removed it, causing HTTP 400)
- **`/settoken` Bearer prefix** — Now handles `Bearer eyJ...` input by stripping the prefix automatically

---

## v1.0.1 — Stats, Status & Ops Improvements (2026-03-26)

### CLI Bot v1.0.1

#### Added

- **Enhanced `cmd_stats()`** — Full per-session details: speed (BPS/BPM peak/low), balance extremes, total wagered, uptime; plus an all-time totals section across all sessions
- **Enhanced `cmd_status()`** — Full session details: strategy, stop conditions, rules, speed ranges, and 30 session metrics
- **`shufflectl start-all / stop-all / status-all`** — Batch commands to start, stop, or check status of all managed services at once

#### Security

- **`_save_state_file()` permissions** — State files are now written with `0o600` permissions, preventing other users from reading session state

### Infrastructure v1.0.1

- **`run-tg.sh` helper** — Added to `install.sh` for launching the Telegram bot process directly

---

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
