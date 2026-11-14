# Changelog

## CLI v1.9.0 — Observability, retention, proxy, async writer, live editing (2026-11-14)

Mirrors WolfBet CLI v2.17.0 (API / cycle / efficiency metrics), v2.18.0 (wizard Step 5b + preset keys), v2.19.0 (`T` tweak + `_apply_mutation`), v2.22.1 / v2.32.1 (`_check_risky_config` incl. chain depth), v2.25.0 (`--config-show` / `--config-set` / `--version`), v2.26.0 (proxy, `purge_days`, `--cleanup`), v2.26.1 (`--benchmark` / `--diagnose`), v2.27.0 (async bet writer), v2.28.1 (monitor footer hints, systemd-aware stop) and v2.30.3–v2.30.5 (windowed metrics). TG unchanged. GraphQL documents, string amounts and `windowId` unchanged.

- **Metrics** — every bet request is timed (`_record_api_ms`) and every full loop iteration is timed (`_record_cycle_ms`). API last / p50 / p99 / avg / peak, on-target % (with a bet delay), cycle p50 / p99 / mean over 500-sample windows, overhead, recent BPS (1000 / cycle mean), theoretical max BPS and efficiency. Shown on two new dashboard rows and in `--status`, and written to the state file (`api_*_ms`, `cycle_*`, `bets_per_second_recent`, `efficiency_pct`, `theoretical_max_bps`, `db_writer_*`, `purge_days`, masked `proxy`).
- **`--benchmark [N]`** — N (default 20, clamped 3-200) read-only `GetMyProfile` queries through the betting session (same proxy and TLS impersonation). Prints min / p50 / mean / p99 / max / ok, theoretical BPS and the shared `benchmark_verdict` (`casino_name="Shuffle"`). No bets, no DB writes. The query string is now the module constant `_PROFILE_QUERY`, shared with `api_get_balances`.
- **`--diagnose`** — bottleneck analysis from the state file only: actual / recent / theoretical BPS, efficiency (`>100%` when the window runs ahead), cycle median / p99, the api + overhead + delay breakdown, the DB writer line, the top blocker and advice.
- **Retention** — `purge_days` (1-30, default 3). The startup purge and `--cleanup [DAYS]` use `cleanup_old_bets`: they delete the bet rows of ended sessions and keep the session rows. An hourly `Retention` thread runs `cleanup_live_bets`.
- **Proxy** — `proxy` config key (`http://`, `https://`, `socks5://`; `off` clears it), checked with `shared.core.net.validate_proxy`. `_create_http` sets `.proxies` on the curl_cffi or the requests session. So the client that `_gql_post` recreates after a connection reset keeps the proxy. A bad saved proxy is dropped with a warning. The proxy is applied on load, on `--config-set` / tweak, and on SIGHUP reload, and always shown masked.
- **Async bet writer** — `db_write_mode` `async` (default) or `sync`. The writer thread batches up to 32 rows or 0.1 s into one commit. When the 1000-row queue is full, the row is written synchronously, so no row is lost. The queue is drained before the session save on SIGTERM, daemon exit and TUI exit. A mode change takes effect on the next start.
- **Live editing** — `T` hotkey opens the runtime tweak prompt (`param value`, WolfBet aliases plus `multiplier` / `mult` → `multiplier_target`; on dice the target is re-derived). `--config-show` (token and proxy credentials masked), `--config-set KEY=VALUE…` (refuses `bearer_token`, prints `old → new` and clamp warnings, lists restart-only keys, sends SIGHUP to a running daemon), `--version`, and a help epilog with examples. Wizard Step 5b (optional): recurring, delays, streak reset, `basebet_pct`, streak tuples, milestones. The confirm step shows `_check_risky_config` heads-ups, with WolfBet keys remapped to shuffle's: Martingale-style = `2` / `6`, rules = `7`. `proxy`, `purge_days` and `db_write_mode` are in the config, preset, live-reload and tweak key lists.
- **Monitor** — a second footer line points at `--config-set` and `shufflectl stats / analytics`. `S` runs `systemctl --user stop shuffle` when the daemon runs in the `shuffle.service` cgroup, so `Restart=` can't respawn it; otherwise it sends SIGTERM.
- N/A: the rate-limit row and its advice (no quota headers), `/auto/start` quota, pipeline depth, auto-bet UUID clearing and `_derive_bet_value` (no server-side auto-bet; a `base_bet` change applies to the next bet). `--benchmark --lanes` and lane shares are E5-S7. The `shufflectl` pass-throughs for these commands are E7.
- Tests: `tests/test_wolfbet_sync_e5.py` (74); `test_portability_v1.py` version pin.

`VERSION` `1.8.0` → `1.9.0`.

## CLI v1.8.0 — Stats windows, durations, analytics, lane filter (2026-11-14)

Mirrors WolfBet CLI v2.23.0 (`--analytics`), v2.30.9 (`--stats --since/--until`), v2.31.0 (date-range analytics), v2.33.0 (time-of-day window fix), v2.33.1 (wins / losses per session), v2.34.0 (durations, time span, run time) and the v2.35.0 stats / analytics lane filter. TG unchanged. GraphQL documents, string amounts and `windowId` unchanged.

- **Window fix** — `--since` / `--until` use `shared.core.stats.stats_date_window`: `--since "2026-11-13 14:00"` now starts at 14:00 instead of matching the whole day (`' ' < 'T'`), a bare `--until` date runs to `23:59:59.999999`, and a bad bound prints `Invalid --since/--until "…"` and no rows. The local `_stats_date_window` is gone.
- **`--stats` totals** — `Sessions: N  (first → last)`, `Time Span … Bet Time …` (span includes the gaps between cycles, bet time does not; `fmt_duration` format `1mo 2d 3h 4m 5s`), `Wins/Session: Max N Avg N.N`, `Losses/Session: Max N Avg N.N`. All honour the window and lane.
- **New `--analytics [--since --until --lane K]`** — Overall panel (sessions, time range, time span, bet time, sessions in profit / loss, bets, win rate, wagered, P/L, best / worst session), By game (game labels), By strategy, By lane (only when the window has 2+ lanes), Top 5 best / worst sessions. Reads only `sessions`.
- **Lanes** — new `--lane K` (1-10) filters `--stats` / `--analytics` on `sessions.lane` (old rows count as lane 1); session headers show `[LK]` for lane 2+. `init_db` adds `sessions.lane` idempotently (`ensure_sessions_lane`), also when `--stats` / `--analytics` open an older DB. Running lanes is not here yet (E5-S7): every new row is lane 1.
- **Run time** — the dashboard header shows `Run …` (time since the first cycle started, never reset across cycles); `--monitor` shows it from `run_uptime_sec` and omits it for older state files; `--status` gains a `Running for` row.
- **`shufflectl analytics`** pass-through (args forwarded, like `stats`).
- N/A: none; every item maps to shuffle's `sessions` table, which has the same columns as WolfBet's.
- Tests: `tests/test_wolfbet_sync_e4.py` (18); `test_portability_v1.py` window tests moved to the shared helper (until bound `T23:59:59.999999`) and version pin.

`VERSION` `1.7.0` → `1.8.0`.

## CLI v1.7.0 — Session cycles, streak reset, live reload (2026-11-14)

Mirrors WolfBet CLI v2.16.0 (recurring, streak delays, `streakbet_loss`, `basebet_pct`, milestones), v2.25.3 (`recurring_delay`), v2.28.2 (config persistence), v2.28.3 (SIGHUP reload), v2.28.4 (uptime freeze), v2.30.1 / v2.30.7 (`next_profit_milestone`, `Ms` window), v2.30.6 (delay cap), v2.32.0 (streak reset) and v2.32.1 (insufficient balance terminal). TG unchanged. GraphQL documents, string amounts and `windowId` unchanged.

- **Recurring cycles** — with `recurring` on, a stop on profit target / max bets / max wins (profit ≥ 0) closes the `sessions` row, pauses `recurring_delay` s (default 60), then opens a new row with fresh counters and streaks. The bumped `base_bet` is kept; `next_profit_milestone` re-arms from `profit_threshold`. Loss, balance-floor and insufficient-balance stops never restart. The pause stops early on `q` / SIGTERM and then opens no new row.
- **Streak reset** — `streak_reset_losses` N (0 = off) stops on the first win after N+ losses while profit ≥ 0 and restarts after `streak_reset_delay` s (default 600), **even when `recurring` is off**.
- **Streak pacing and sizing** — `streak_delay_loss` / `streak_delay_win` / `streak_delay_bets` ("N:sec") pause before the next bet; `streakbet_loss` ("N:mult") scales the next bet on every N-th loss in a row (never below `base_bet`); `basebet_pct` sets `base_bet` to that share of the balance every 10 bets; `milestone_bets/wins/losses/profit` log and show a milestone.
- **Uptime** — `uptime_sec` freezes at the last bet during a cycle pause, and `ended_at` uses that time, so bet time excludes the pause. New state-file fields: `run_uptime_sec` (never resets), `pause_remaining_sec`, `pause_total_sec`, `recurring`, `recurring_delay_sec`, `streak_reset_*`, `next_profit_milestone`. Dashboard and `--monitor` show `◐ PAUSED left/total`.
- **CFG row** — fix: `Ms` read `last_profit_milestone`, which was never set, and `Rec` never showed because the state file never wrote `recurring`. `Ms` now shows progress inside the current bump window from `next_profit_milestone`; new `SR nL→Ss` segment.
- **Persistence** — every key above is in the config file and presets; streak tuples are stored as `"N:X"` (null when off). `recurring_delay` and `streak_reset_delay` are clamped to 0..86400 on load and reload, with a message.
- **SIGHUP** reloads the live-safe keys (stops, pacing, recurring / streak reset, milestones, profit ramp, rules) without touching the session, counters or the bet ladder; other changed keys are logged as needing a restart. SIGUSR1 / SIGUSR2 pause / resume unchanged.
- **Fix: insufficient-balance match** — any API error containing "balance" stopped the bot as insufficient. It now matches "insufficient" only. Insufficient balance stays terminal: no recurring or streak-reset restart.
- N/A: server auto-bet UUID clearing and the mid-chain auto guard (no server auto-bet), `--pipeline`, lanes (E5). The `martingale_chain_depth` warning lands with `_check_risky_config` (E5). No wizard step yet for the new keys (WolfBet Step 5b, E5): set them in the config file and send SIGHUP.
- Tests: `tests/test_wolfbet_sync_e3.py` (25); `test_portability_v1.py` version pin.

`VERSION` `1.6.1` → `1.7.0`.

## TG v1.9.0 — Streak reset, delay caps, analytics window (2026-11-14)

Mirrors WolfBet TG 1.20.0: v2.30.6 (delay cap), v2.32.0 (streak reset), v2.32.1 (insufficient balance terminal), v2.33.0 (time-of-day window). CLI unchanged. GraphQL documents unchanged.

- **Streak reset** — `/set srloss N` (alias `streakreset`, `off` to disable) stops the session on the first win after N+ consecutive losses, only while profit ≥ 0. The bot then starts a new session (new `sessions` row) after `/set srdelay S` (alias `streakresetdelay`, default 600 s, 5–86400), **even when `recurring` is off**. Live `/set srloss|srdelay` carries into the next cycle; the bumped `base_bet` is kept and `next_profit_milestone` re-arms from `profit_threshold`. The streak is recorded where the win/loss is decided, before the bet is queued and before the profit-increment step. Uses `shared.core.session_rules` (`record_outcome`, `streak_reset_triggered`, `STREAK_RESET_REASON`).
- **Delay caps** — `/set recdelay` and `/set srdelay` clamp to 5..86400 and tell you when they clamp. A saved `recurring_delay` above 24 h is capped at the restart.
- **`/tweak`** gains `srloss` (live on the running engine) and `srdelay` / `recdelay` (saved; apply at the next stop).
- **`/config`, `/status`, stop message** show `Streak reset … / SR nL→Ss` when set; `/help` lists the keys; web settings page lists `streak_reset_losses` / `streak_reset_delay`; both keys are in `CONFIG_KEYS` (presets) and the `/resume` snapshot.
- **`/analytics`** builds its window with `shared.core.stats.stats_date_window`: `/analytics 2026-11-13 14:00` now starts at 14:00 instead of matching the whole day (`' ' < 'T'`). A time token after a date joins it, since Telegram splits on spaces. Bad bounds print the usage.
- **Fix: insufficient-balance match.** The GraphQL `errors` path and the `ConnectionError` path stopped the session on any message containing "balance" (`"insufficient" or "balance"`), the same bug as goated. They now match "insufficient" only, so e.g. a currency error no longer ends the session as insufficient. Insufficient balance stays terminal: no recurring or streak-reset restart. (The CLI has the same match; fixed in its E3 story.)
- N/A for TG: none of the v2.32.0–v2.33.0 TG items depend on WolfBet-only API features.
- Tests: `tests/test_wolfbet_sync_e2.py` (28); fake gains `api_error_balance`. `test_portability_v1.py` version pin and the `/analytics` source check updated for the shared helper.

`tg/__init__.py` `1.8.1` → `1.9.0`.

## CLI v1.6.1 / TG v1.8.1 — casino-shared over HTTPS (2026-11-13)

Mirrors wolfbet v2.16.11 (casino-shared dependency installs over HTTPS). No bot behaviour change.

- **`requirements.txt`, `web/requirements.txt`: `casino-shared` via `git+https://github.com/snad1/casino-shared.git` instead of `git+ssh://`.** The repo is public, so pip fetches it anonymously; fresh servers no longer need a GitHub SSH key to run `install.sh`.
- **`shufflectl update`** reinstalls casino-shared from the same HTTPS URL (it used `git+ssh` too).
- **Pinned to casino-shared `v0.2.0`** (`@v0.2.0`): the WolfBet sync helpers (`shared.core` session rules, stats, diagnostics, net, lanes and `sessions.lane`). `shufflectl update` still reinstalls the latest `main`, as `wolfctl` does.

`VERSION` `1.6.0` → `1.6.1`. `tg/__init__.py` `1.8.0` → `1.8.1`.

## CLI v1.6.0 / TG v1.8.0 — Date-range filters (2026-08-15)

Mirrors wolfbet v2.30.9. Both surfaces now filter analytics to a date window.

- **CLI `--since` / `--until`** (standalone parser args, used with `--stats`) — bare `YYYY-MM-DD` or full ISO timestamps; a bare `until` is extended to end-of-day so the day is inclusive. Shared helper `_stats_date_window()` builds the WHERE clause; both the session list (limit 50 when filtering, 20 otherwise) and totals honor the window. Headers show the window and switch to "Range Totals".
- **`shufflectl stats`** now forwards extra args — `shufflectl stats --since 2026-08-01 --until 2026-08-15` works.
- **TG `/analytics [SINCE] [UNTIL]`** — all 5 queries (lifetime, by-game, by-strategy, top-5 best/worst) honor the window; header shows `Range analytics` + window label. `>2` args prints a usage hint. `/help` updated.

`VERSION` `1.5.0` → `1.6.0`. `tg/__init__.py` `1.7.0` → `1.8.0`. Tests: 21 → 27.

## CLI v1.5.0 / TG v1.7.0 — WolfBet hardening subset ported (2026-07-19)

Same portable subset as cryptogames + duckdice v1.5.0. No bet-loop change.

- **SQLite PRAGMAs** on both CLI `_db_conn` and TG `_get_conn` (synchronous=NORMAL, busy_timeout=5000, cache_size=-32000, temp_store=MEMORY).
- **`Restart=on-failure`** in `install.sh` for both units.
- **Uptime pause bookkeeping** — `session_bet_ended_at` + `session_pause_started_at`; `pause()`/`resume()` freeze/clear; `get_status()` uses frozen bet-end; `_db_save_session(final=True)` writes frozen `ended_at`. Emits `uptime_sec` + `pause_remaining_sec` on the status dict.
- **Live monitor** — real sparkline preserved (v1.4.0 behaviour); new config-progress row `CFG …` (Ms/MaxP/MaxL/MaxB/MaxW/MinBal/Rec) added immediately after it. `bets_avail` budget adjusted from `term_h - 10` to `term_h - 11`.
- **State-file monitor** — old placeholder `" P/L —"` replaced by the same config-progress row.
- **Both monitors** share `_build_config_progress_row(d)` + `_pct_color()` — green <80%, yellow ≥80%, bold_red ≥95%; fallback `CFG — no goals set` when nothing armed.
- **TG `/diagnose`** + **TG `/analytics`** + `/help` entries.
- **First tests**: `tests/test_portability_v1.py` — 21 tests.

`VERSION` `1.4.0` → `1.5.0`. `tg/__init__.py` `1.6.10` → `1.7.0`. `shufflectl` unchanged.

## v1.6.10 — `/status` speed block trimmed (2026-06-17)

### Changed

- **Removed `Overhead` line** from the `/status` speed block — rarely actionable at a glance, adds noise.
- **`API stable` and `Efficiency` now render only when `bet_delay > 0`.** With no delay set, the theoretical-max reference is meaningless and median-vs-target has no target — both lines were pure clutter. When the user does set a delay, both lines render as before.

## v1.6.9 — Session-ended notification: green ✅ on profit, red 🛑 on loss (2026-06-17)

### Changed

- **`Session #X ended` notification now leads with `✅`** when the session ended with `profit >= 0`, keeping `🛑` for `profit < 0`. Previously every stop-notification opened with `🛑` regardless of outcome — a `max_profit` hit still triggered a red-icon push. Leading emoji is now driven by the sign of `s["profit"]`; every other line is unchanged.

## v1.6.8 — Fix: `/status` rendering as plain text — `bet_delay` opened an unclosed italic entity (2026-06-17)

### Fixed

- **`/status` rendered as PLAIN TEXT** (literal `*Session #X*`, `*Balance*`, `` `…` `` visible) because the v1.6.7 fallback was firing every invocation. Root cause: the literal token `bet_delay` on the "API stable" line sat OUTSIDE any code span — Markdown V1 read the `_` as the start of an italic entity, never found the closing `_`, and rejected the message. Renamed to `bet delay` (space, not underscore) — same meaning, no Markdown bomb. The v1.6.7 fallback remains in place as the safety net.

## v1.6.7 — Fix: `/status` silently failing on Markdown parse error (2026-06-17)

### Fixed

- **`/status` returns nothing in Telegram** when a rule description or engine status string contains an unbalanced Markdown character. `format_status` wraps user/external strings inside Markdown V1 code spans (`` `…` ``); a stray backtick splits the entity and Telegram silently rejects the message. Fixed at the render boundary — `_safe_code(s)` escapes backticks in the two leaky fields (rule descriptions, trailing engine status). Added `safe_reply_markdown(...)` wrapper for `/status` and `/monitor`; enhanced `_safe_send` and the monitor-refresh `edit_text` paths to retry as plain text on parse-error `BadRequest`. The user always sees the message; technical detail is logged server-side only.

## v1.6.6 — Fix: SQLite cross-thread error in _get_conn (defensive) (2026-06-17)

### Fixed

- **`DB flush_bets failed: SQLite objects created in a thread can only be used in that same thread.`** — `_get_conn` now opens directly with `sqlite3.connect(..., check_same_thread=False)` instead of via shared `db_connect`, so a stale pip-installed casino-shared can't reintroduce the bug.

## v1.6.5 — API stable: median + on-target % (2026-06-17)

### Added

- **`API stable: Xms median / Y% on-target`** — median = typical API time (unbiased by spikes); on-target % = bets where API ≤ bet_delay. Explains the Efficiency ceiling.

## v1.6.4 — Efficiency % indicator (2026-06-17)

### Added

- **`Efficiency: X%`** on the dashboard — bot's actual recent bps as a % of the theoretical max (= 1 / max(bet_delay, api_avg)). Single number that answers "is the bot at peak?".

## v1.6.3 — API peak time on the dashboard (2026-06-17)

### Added

- **`API: X last / Y avg / Z peak`** — third field tracks the slowest single API response observed this session.

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
