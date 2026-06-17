# Changelog

## v1.6.2 — Cycle metric: full per-bet time (bps reciprocal) (2026-06-17)

### Added

- **`Cycle: Xms/bet`** on the dashboard — exp-smoothed time between consecutive bet starts. This is the bps reciprocal (`Cycle × bps ≈ 1000`), shown inline as `(1000/385 = 2.60 bps)`. Complements `Overhead` (post-API processing only) — `Cycle` is the honest end-to-end number.

## v1.6.1 — Per-cycle overhead diagnostic + get_status() cache (2026-06-17)

### Added

- **`Overhead: Xms/bet`** on the dashboard — exp-smoothed time per bet OUTSIDE of `bet_delay` and the API call. Healthy = ~5–15ms; 50ms+ = investigate.
- **`get_status()` cache (250ms TTL)** — eliminates the brief per-second bucket dip at milestones.

## v1.6.0 — Sliding-window BPS + non-blocking SQLite writes (2026-06-17)

### Added

- **`Now` bps (sliding 60s window)** alongside the existing cumulative `Avg` — dashboard now shows both.

### Changed

- **DB writes no longer block the event loop.** `_flush_bets`, `_db_save_session`, and `cleanup_live_bets` now run via `asyncio.to_thread`. Inline batch-size flush trigger removed. Shared `db_connect` now opens with `check_same_thread=False`.

Result: multiple concurrent sessions scale closer to single-session throughput.

## v1.5.0 — Auto-revive: systemd retry-forever + external stall watchdog (2026-06-17)

### Added

- **`Restart=always` + `StartLimitIntervalSec=0`** on both CLI and TG bot services.
- **External stall watchdog timer** — `shuffle-tg-watchdog.timer` fires every 2 min; if `/tmp/shuffle-tg.heartbeat` is older than 180s, restarts `shuffle-tg`.
- **Heartbeat task** — `_heartbeat_loop()` writes the heartbeat file every 30s from `post_init`.

If the bot crashes, systemd brings it back in 10s. If it stalls, the watchdog brings it back within ~3 min. No more SSH-and-restart.

## v1.4.6 — /update cd's into repo dir before running ctl (2026-06-11)

### Fixed

- **/update failed with "No shuffle.py in current directory"** — `systemd-run` spawns bash with cwd=/. Now `cd /root/shuffle && shufflectl update` is the bash command. Override via `BOT_REPO_DIR` env var if your layout is different.

## v1.4.5 — /update reports real errors + writes update log (2026-06-10)

### Fixed

- **/update silently failed when systemd-run errored** — Previous `subprocess.Popen` with `stderr=DEVNULL` swallowed failures. Now uses `subprocess.run --no-block` synchronously, captures stdout/stderr, surfaces real errors to Telegram. Update output goes to `/tmp/shufflectl-update.log` so you can review what happened: `cat /tmp/shufflectl-update.log`.

## v1.4.4 — /update Telegram command for self-update (2026-06-10)

### Added

- **`/update` Telegram command (owner-only)** — Pull latest code from git, copy files, reinstall `casino-shared`, restart the bot. Equivalent to running `shufflectl update` over SSH, but triggered from Telegram. Owner gate via `TG_OWNER_ID` env var (defaults to your user ID if unset). Implementation spawns `shufflectl update` via `systemd-run --user --collect` so the update survives the bot getting killed by the restart.

## v1.4.3 — Remove 5-session concurrency limit (2026-05-12)

### Changed

- **Unlimited concurrent sessions per user** — `MAX_SESSIONS = 5` cap removed from `/bet` and from the recurring-restart path. Users can now run as many concurrent sessions as they want (limited only by the casino's own API rate limits, which are shared across sessions).

## v1.4.2 — bet_delay is a minimum-interval, not a sleep-before (2026-05-11)

### Changed

- **`bet_delay` semantics** — Previously: `sleep(bet_delay)` before each bet, so cycle = `bet_delay + api_time`. Now: minimum interval since the previous bet *started*, so cycle = `max(bet_delay, api_time)`. With delay=1s and api=50ms, you get a clean 1 bet/sec; with api=1500ms there's no extra sleep so the bot keeps up. More predictable pace.

## v1.4.1 — Fix _safe_send infinite recursion (2026-05-02)

### Fixed

- **`_safe_send` infinite recursion** — The function was calling itself instead of `app.bot.send_message`, causing `RecursionError: maximum recursion depth exceeded` on every Telegram notification (stops, milestones, errors). Fixed to call `app.bot.send_message` directly.

## v1.4.0 — Recurring honors live config across all running sessions (2026-04-28)

### Fixed

- **Recurring only applied to sessions started AFTER `/set recurring on`** — Previously, `recurring_state` was registered at `/bet` time only if `config.get("recurring")` was true at that moment. If a user started a session, then later toggled recurring on and started another session, only the second one would auto-restart on stop — the first had no recurring entry. Now `recurring_state` is registered for every session unconditionally (per-slot snapshot of game/strategy preserved). At stop time the engine re-reads the user's saved config to decide whether to actually fire the restart, so toggling `/set recurring on/off` applies instantly to **every** running session regardless of game type.
- **Recurring lost across bot restart** — `load_resume_state` now also registers `recurring_state` for each resumed session so a stop after a bot update still triggers the configured restart.
- **Notification throttle now driven by live config** — The "is this a recurring session" check used for stop-message rate-limiting now reads the live `recurring` flag instead of the snapshot, matching the new behavior.

Per-session game/strategy isolation is preserved: each session's snapshot keeps its own game/strategy/rule etc., so a session restart stays the same game even with multiple games running concurrently.

## v1.3.0 — Streak Delay Bets (2026-04-21)

### Added

- **`streakdelay_bets`** — Delay every N total bets by X seconds, regardless of win/loss outcome. Format `N:seconds` (e.g. `/set streakdelay_bets 100:0.5` pauses 0.5s every 100 bets). Useful for throttling overall bet rate independent of streaks. Live-tweakable via `/tweak sdbets 100:0.5`. When multiple streak delays apply on the same bet (e.g. loss streak + bet count), the longest delay wins

## v1.2.2 — Recurring Notification Throttle (2026-04-21)

- **Telegram flood ban on fast recurring sessions** — Stop and start notifications are now rate-limited to at most one of each per 30 seconds per user during recurring cycles. Losses, errors, insufficient balance, and manual stops are always sent immediately regardless of throttle

## v1.2.1 — Recurring Restart Reliability & Task Safety (2026-04-21)

### Fixed

- **Fire-and-forget task exceptions now logged** — Added `_log_task_exception` done-callback to all `create_task` calls so unhandled exceptions are surfaced in logs instead of being silently swallowed
- **`_notify_stop` formatting errors caught** — Wrapped `format_stop` call in try/except; falls back to plain stop message so the bot never crashes on a bad status snapshot
- **`ensure_future` replaced with `create_task`** — All callback factories (`_make_on_stop`, `_make_on_milestone`, `_make_on_error`) now use `asyncio.create_task` with done-callbacks for proper error visibility
- **Recurring restart retries on engine-start failure** — When `engine.start()` returns false, the session is re-queued after the configured delay instead of being silently dropped
- **Monitor loop stays alive during restart gap** — `_monitor_loop` now continues iterating while a recurring restart timer is pending, showing "Recurring restart pending…" instead of prematurely exiting
- **Inline keyboard preserved during restart gap** — `refresh_status` callback keeps Refresh/Stop buttons visible with a pending message while waiting for the next recurring restart

## v1.2.0 — Adaptive Base Bet & Full Config Visibility (2026-04-14)

### Added

- **`basebet_pct`** — Set base bet as a fraction of current balance (e.g. `/set basebet_pct 0.001` = 0.1%). Recomputes on session start and at every profit-increment milestone, so base bet auto-shrinks during drawdowns and grows during profit. Live-tweakable via `/tweak bbpct 0.001`
- **`streakbet_loss`** — Reduce (or scale) bet after N consecutive losses. Format `N:multiplier` (e.g. `/set streakbet_loss 10:0.5` halves bet every 10 losses). Damping floor for Martingale-style strategies. Live-tweakable via `/tweak sbloss 10:0.5`

### Changed

- **Full config in every surface** — Session-start message, `/status`, and stop summary now display every active setting: streak delays, streak bet, basebet pct, profit bump + next milestone, milestone cadence, stops, pipeline, rule/value, win/loss multipliers. Single `format_full_config` helper in formatter; no more drift between surfaces

## v1.1.4 — Insufficient Balance Stop (2026-04-13)

### Fixed

- **Insufficient balance now always stops session** — Added pre-bet balance guard (`current_bet > current_balance`). Previously, deep Martingale streaks could drive `current_bet` above balance without the session stopping

## v1.1.3 — Streak Delay & Recurring Bet Fixes (2026-04-09)

### Added

- **Streak delay** — Delay next bet after N consecutive wins/losses. Configure with `/set streakdelay_loss 5:1.0` (every 5 losses → 1s delay) and `/set streakdelay_win 10:0.5`. Works with all strategies including auto-bet. Live tweak with `/tweak sdloss 5:1.0` / `/tweak sdwin off`
- **Streak delay in /config** — Displays streak delay settings in configuration output

### Fixed

- **Recurring bet preserves profit increment** — When a session stops and recurring restarts, the current (profit-incremented) base_bet is now carried forward instead of resetting to the original config value. Useful for short recurring sessions to bypass server-side throttling while maintaining bet progression

## v1.1.2 — Bug Fixes (2026-04-07)

### Fixed

- **Live bet cleanup** — ISO timestamp `T` separator caused comparison failure with SQLite's `datetime()`. Bets older than purge_days now correctly deleted during running sessions
- **Telegram timeout on session start** — Session start confirmation wrapped in try/except. Session continues even if Telegram times out
- **Help text** — Added missing cleanup, delsession, purgedays commands

---

## v1.1.1 — Safety, Cleanup, Charts (2026-04-03)

### Added

- **Live bet cleanup** — Bets older than N days auto-deleted every hour during running sessions. `/set purgedays 1` (default 1, range 1-30)
- **Chart snapshots** — Profit/balance saved every 100 bets. Web charts survive bet cleanup
- **Profit increment = 0** — Creates new sessions at profit threshold without increasing base bet

### Fixed

- **CK audit** — 65 scientific notation violations fixed across all services
- **`/tweak basebet` safety** — No longer kills strategy recovery during loss streak (applies on next win)

---

## v1.1.0 — Async Engine, /tweak, Proxy (2026-04-02)

### Added

- **Async engine** — TG betting engine converted from blocking threads to async I/O. Multiple sessions run truly concurrent instead of competing for Python's GIL
- **`/tweak` command** — Live-edit running sessions without stopping: delay, stop conditions (maxwins, maxbets, maxprofit, maxloss, minbalance), milestones, basebet, multiplier, loss/win mult
- **Per-session proxy** — Route each session through a different IP: `/set proxy http://user:pass@ip:port` or `socks5://...`
- **Production-safe errors** — `_set_error()` method ensures raw API errors never leak to users in production

### Changed

- HTTP client: `requests.Session` → `curl_cffi.AsyncSession`
- Threading: `threading.Thread` → `asyncio.create_task`
- Sleep: `time.sleep` → `asyncio.sleep`
- Callbacks: `run_coroutine_threadsafe` → `asyncio.ensure_future`
- Suppressed httpx/httpcore polling log noise

---

## v1.0.7 — Recurring Bets & Zero-Balance Start (2026-03-29)

### Added

- **Recurring bet feature** — Sessions auto-restart after configurable delay when stopped by conditions (profit target, max bets, etc). Configure with `/set recurring on|off` and `/set recurringdelay <seconds>`. Cancel with `/stop recurring`

### Fixed

- **TG engine zero-balance start** — Allow zero-balance sessions when base bet is 0 (previously blocked with "No balance found")

---

## v1.0.6 — Fix BPM Calculation (2026-03-29)

### Fixed

- **BPM calculation** — `bets_per_minute` was set to raw count of bets in the current calendar minute instead of the actual rate; now correctly derived as `bets_per_second * 60`

---

## v1.0.5 — Zero Bets & Insufficient Balance Fix (2026-03-29)

### Fixed

- **Zero-amount bets** — Allow base bet of 0 for free/test betting; MIN_BET guard changed from `amount < MIN_BET` to `0 < amount < MIN_BET` so zero passes through to the API
- **Default base bet** — Changed default `base_bet`/`current_bet` from `MIN_BET` to `0.0`
- **Insufficient balance detection** — CLI and TG now properly detect "insufficient balance" in HTTP error responses and stop the session immediately
- **TG error notification** — Added `on_error` callback; sends `⚠️ Insufficient balance` message to user via Telegram before stopping

---

## v1.0.4 — Shared Library as pip Package (2026-03-29)

### Infrastructure

- **casino-shared pip package** — `shared/` library now installable via `pip install casino-shared` from GitHub, eliminating manual file copying on servers
- **requirements.txt** — Added `casino-shared @ git+ssh://git@github.com/snad1/casino-shared.git` to both bot and web requirements
- **install.sh** — Added `shared/` copy step as fallback for offline/non-pip installs
- **shufflectl update** — Now syncs `shared/` library alongside core/tg during updates

---

## v1.0.3 — DRY Shared Library Extraction (2026-03-29)

### Infrastructure

- **Shared library extraction** — Extracted ~6,600 lines of duplicated code into `shared/` library used across all 6 casino bots
  - `web/websocket.py`, `web/database.py`, `web/auth.py`, `web/services.py`, `web/bot_db.py`, `web/routes/auth_routes.py` — thin shims delegating to shared modules
  - `tg/database.py` — thin shim binding `DATA_DIR` to shared persistence
  - `core/strategy.py` — keeps local STRATEGIES dict, delegates rule engine to shared
  - `core/database.py` — keeps local `init_db`, delegates utilities to shared
- Zero downstream import changes — all existing imports continue working

### Security

- **Shell injection fix** — Replaced `create_subprocess_shell` with `shutil.copy2` in update flow
- **Deprecated datetime fix** — Replaced `datetime.utcnow()` with `datetime.now(timezone.utc)`
- **Narrowed exception handling** — Replaced broad `except Exception` with specific types
- **Removed unused imports** — Cleaned up unused `import os` from shims

### Testing

- **114-test suite** added covering all shared modules

---

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
