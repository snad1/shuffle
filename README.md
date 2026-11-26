# Shuffle AutoBot v1.13.0 + Telegram Bot v1.10.1 + Web Admin v0.1

High-speed multi-game auto-betting engine for Shuffle.com (Limbo + Dice) with a live terminal dashboard, a Telegram bot, and a web admin panel. Uses GraphQL API with Bearer token auth and curl_cffi Chrome TLS fingerprinting for server deployments.

---

## Features

| Feature | Details |
|---|---|
| **Pure ANSI TUI** | Direct ANSI rendering (no Rich flicker), fits any terminal, refreshes 2x/sec |
| **Multi-game support** | Limbo + Dice via pluggable game registry — add new games in ~20 lines |
| **7 betting strategies** | Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Delay Martingale, Rule-Based |
| **Rule-based engine** | Build custom IF/THEN rules with interactive wizard — sequence, profit, and bet conditions |
| **Editable multipliers** | Customize loss/win multipliers per strategy (no more hardcoded 2x) |
| **Named presets** | Save & load strategy configs by name with `--preset` |
| **GraphQL API** | Single endpoint, Bearer token auth — no CloudFlare bypass needed |
| **curl_cffi Chrome TLS** | Bypasses server TLS fingerprinting with retry-on-reset |
| **Balance API** | Fetches real balances via GraphQL `GetMyProfile` query |
| **Smart stop conditions** | Max profit, max loss, max bets, max wins, min balance floor |
| **Safety cap** | Never bets more than 20% of current balance in one go |
| **Session database** | SQLite log of every bet and session — survives restarts |
| **Auto data retention** | Old bet records auto-purged after 3 days; session stats preserved forever |
| **Config persistence** | Save/load config with `--resume`, smart wizard with defaults |
| **Monitor mode** | `--monitor` to attach a live TUI to a running daemon — pause/resume/stop remotely |
| **Daemon mode** | `--daemon` for background running, `--status` to check, `--stop` to halt |
| **Performance tracking** | BPS, BPM, peak/low speed ranges, peak/low balance, best win, worst loss, avg P/L |
| **Test mode** | Set bet amount to `0` for free bets (no real money risked) |
| **Daily log rotation** | Logs at `~/.shuffle_logs/shuffle.log`, rotates at midnight, 30-day retention |
| **Interactive keyboard** | `P` pause, `R` resume, `H` history, `Q` quit — all from dashboard |

---

## Quick Start

### Mac / Local

```bash
# 1. Setup
cd shuffle
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt  # includes casino-shared from GitHub

# 2. Run (wizard guides you through config)
python3 shuffle.py

# 3. You'll need from your browser DevTools (F12 → Network tab → any GraphQL request):
#    - Authorization header value (Bearer token starting with 'eyJ...')

# 4. Resume with saved config
python3 shuffle.py --resume
```

### Remote Server

```bash
# 1. Upload files
scp -r shuffle/ user@your-server:~/shuffle

# 2. Install (sets up venv, systemd)
ssh user@your-server
cd ~/shuffle && chmod +x install.sh && ./install.sh

# 3. Configure
shufflectl setup

# 4. Start & monitor
shufflectl start
shufflectl monitor
```

---

## Games

| Game | Mutation | Parameters |
|---|---|---|
| **Limbo** | `LimboPlay` | `amount`, `bet` (multiplier target), `currency`, `windowId`, `usdAmount` |
| **Dice** | `DicePlay` | `amount`, `bet` (multiplier target), `currency`, `direction` (ABOVE/BELOW), `usdAmount` |

---

## Strategies

| # | Name | How it works | Risk |
|---|---|---|---|
| 1 | **Flat Bet** | Same amount every time | Very Low |
| 2 | **Martingale** | Multiply on loss, reset on win (configurable multiplier) | High |
| 3 | **Anti-Martingale** | Multiply on win, reset on loss (configurable multiplier) | Medium |
| 4 | **D'Alembert** | +1 unit on loss, -1 on win | Low-Medium |
| 5 | **Paroli (3-step)** | Multiply on win up to 3x then reset (configurable multiplier) | Medium |
| 6 | **Delay Martingale** | Flat for N consecutive losses, then start multiplying | High |
| 7 | **Rule-Based** | Build custom IF/THEN conditions & actions (see below) | Variable |

---

## CLI Options

```bash
python3 shuffle.py                   # Interactive wizard + TUI dashboard
python3 shuffle.py --resume          # Skip wizard, reuse saved config
python3 shuffle.py --preset X        # Load preset X and start
python3 shuffle.py --daemon          # Run in background (no TUI, implies --resume)
python3 shuffle.py --monitor         # Attach live TUI to running daemon
python3 shuffle.py --status          # Check running session status (one-shot)
python3 shuffle.py --stop            # Stop a running daemon
python3 shuffle.py --setup-only      # Run wizard, save config, don't start
python3 shuffle.py --list-presets    # Show saved presets
python3 shuffle.py --stats           # Show all session history + totals + uptime
                                     #   add --since DATE / --until DATE for a date window
python3 shuffle.py --last-bets N     # Show last N bets across sessions
python3 shuffle.py --session-bets N  # Full stats + streak distribution for session N
python3 shuffle.py --version            # Print the version
python3 shuffle.py --analytics          # Aggregates: by game, strategy, lane, top 5 best / worst (--since / --until / --lane K)
python3 shuffle.py --config-show        # Print the saved config (secrets masked)
python3 shuffle.py --config-set maxloss=5 delay=0  # Edit saved config; a running daemon reloads live keys (SIGHUP)
python3 shuffle.py --config-set piscope=run pi=0.0001 pt=0.001  # Run-wide profit increment (piscope=session keeps it per session)
python3 shuffle.py --benchmark 20       # Time N read-only probe requests (no bets)
python3 shuffle.py --benchmark 20 --lanes 4  # Also probe 1..M concurrent clients and recommend a lane count
python3 shuffle.py --diagnose           # Bottleneck analysis of the running daemon
python3 shuffle.py --cleanup [DAYS]     # Purge bet rows of sessions ended > DAYS ago (default purge_days)
python3 shuffle.py --config-set lanes=2 lane_split=budget  # Parallel lanes: count + money split (none / budget / bet / both)
python3 shuffle.py --daemon --lane 2    # Run lane 2 (--status / --monitor / --stop / --stats --lane K act on lane K)
python3 shuffle.py --run-reset [--lane K]  # New run total for the run-wide profit increment
python3 shuffle.py --export ~/s12.csv --export-session-id 12  # Export a session (.json = JSON, else CSV; default latest)
python3 shuffle.py --backtest --simulate 10000 --seed 42  # Offline backtest: no API calls, no real balance
python3 shuffle.py --backtest --replay-session 12  # Replay a stored session's outcomes offline
python3 shuffle.py --verbose            # Debug logging (dev mode)
```

---

## shufflectl (Server Management)

After running `install.sh`, the `shufflectl` command is available:

```bash
shufflectl setup          # Run wizard to configure
shufflectl start          # Start bot as background daemon
shufflectl stop           # Stop the bot
shufflectl restart        # Restart the bot
shufflectl monitor        # Attach live TUI to running daemon
shufflectl status         # Quick status snapshot
shufflectl logs           # Stream live logs
shufflectl logs-full      # Show last 200 log lines
shufflectl interactive    # Start daemon + attach monitor
shufflectl tmux           # Monitor in detachable tmux session
shufflectl stats          # View all-time statistics (--since/--until for a window)
shufflectl session ID     # Full stats + streak distribution
shufflectl presets        # List saved presets
shufflectl update         # Update bot from current directory; each lane restarts on the new code at its session end
shufflectl update --critical   # Same, but restart every lane now
shufflectl lanes [N] [--split none|budget|bet|both] [--max N]  # Show or set parallel lanes; only the difference starts or stops
shufflectl lanes --json                                        # Every lane's state as one JSON document (Telegram reads it)
shufflectl stats --json [--lane K] [--before ID]               # Recent sessions as JSON, older than #ID when given
shufflectl lane add NAME [--from K] | remove K | rename K NAME  # Change one lane; the others keep betting
shufflectl start|stop|restart K   # One lane alone (lane 1 is the shuffle service)
shufflectl restart K --at-boundary   # Restart lane K when its session ends (--cancel-boundary drops it); lane remove K --after-session removes it then
shufflectl restart --rolling         # Restart every running lane at its own session end; starting or restarting lane 1 also starts the enabled lanes that are not running
shufflectl key list | add ID [--label L] [--kind key|proxy|telegram] | rotate ID | delete ID  # Vault tokens, proxies, Telegram token (hidden prompt)
shufflectl config-set lane3.key_ref=friend-a lane3.currency=trx lane3.proxy_ref=friend-px  # Lane 3 on a friend's account; only lane 3 restarts
shufflectl diagnose         # Bottleneck analysis of the running session
shufflectl benchmark [N] [--lanes M]  # Time N probe requests
shufflectl doctor [--fix] [--yes]  # Health check; --fix applies safe fixes
shufflectl start-all        # Start CLI Bot + Telegram Bot + Web Admin
shufflectl stop-all         # Stop  CLI Bot + Telegram Bot + Web Admin
shufflectl status-all       # Show status of all three services
shufflectl config-set KEY=VALUE ...  # Change saved keys; a running daemon reloads
shufflectl analytics [--since DATE] [--until DATE] [--lane K]  # Aggregates: by game, strategy, lane, top 5
shufflectl last-bets [N]    # Last N bets across all sessions (default 20)
shufflectl export PATH [SID]  # Export a session (CSV, or JSON by .json extension)
shufflectl backtest [args]  # Backtest / replay offline (no API calls, no real balance)
shufflectl cleanup [DAYS]   # Purge bet rows of old sessions (default: purge_days)
shufflectl reset [--yes]    # Archive the stats DB and start fresh (never deletes)
shufflectl archives         # List archived stats DBs
shufflectl run-reset [--yes]  # New run total for the run-wide profit increment
shufflectl refresh          # Reinstall shufflectl from the repo copy in this folder
shufflectl version          # Bot and shufflectl versions

# Telegram bot
shufflectl tg setup       # Enter your bot token
shufflectl tg start       # Start as systemd service
shufflectl tg stop        # Stop TG bot
shufflectl tg restart     # Restart TG bot
shufflectl tg status      # Check TG bot status
shufflectl tg logs        # Stream TG bot logs

# Web admin
shufflectl web start      # Start web app
shufflectl web stop       # Stop web app
shufflectl web restart    # Restart web app
shufflectl web status     # Show web app status
shufflectl web logs       # Stream web app logs
shufflectl web update     # Update web app files
```

---

## Telegram Bot

The Telegram bot (`shuffle/tg/`) provides the same engine via Telegram commands. Multi-tenant: each user gets their own config, database, and session.

### Commands

| Command | Description |
|---|---|
| `/settoken <bearer_token>` | Set Shuffle Bearer token (message auto-deleted) |
| `/balance` | Check all balances |
| `/config` | View current configuration |
| `/set <param> <value>` | Set a parameter (see `/help` for full list) |
| `/strategies` | List all strategies |
| `/bet` | Start betting session (up to 5 concurrent) |
| `/stop [slot\|all]` | Stop session |
| `/pause [slot\|all]` / `/resume [slot\|all]` | Pause/resume betting |
| `/status [slot]` | Live status |
| `/monitor [slot] [interval]` | Auto-updating status (3-60s) |
| `/stats` | Session history (last 10) |
| `/analytics [since] [until]` | Lifetime or date-windowed analytics (e.g. `/analytics 2026-08-14`) |
| `/session <id>` | Full session report |
| `/lastbets [n]` | Recent bets |
| `/rules` / `/addrule` / `/delrule` / `/editrule` / `/clearrules` | Manage rules |
| `/presets` / `/savepreset` / `/loadpreset` | Manage presets |
| `/cleanup [days]` | Purge old bet records |
| `/delsession <id>` | Delete a specific session |
| `/start` | Welcome and quick start guide |
| `/update` | Pull the latest code and restart the bot (owner only) |
| `/tweak <param> <value>` | Change a setting of a running session (e.g. `/tweak delay 0.1`) |
| `/diagnose` | Bottleneck analysis of a running session |
| `/web` | Open web dashboard |
| `/benchmark` | Test API response speed |
| `/runreset` | Start a new run total for `piscope run` (asks to confirm; stop sessions first) |

---

## Web Admin Dashboard

The web admin (`shuffle/web/`) provides a browser-based dashboard for monitoring users, sessions, and system health.

### Setup

```bash
# Server
cd ~/shuffle && chmod +x web/install.sh
web/install.sh --domain your-domain.com

# Or via shufflectl
shufflectl web start
```

### Features

- Admin dashboard with platform stats and system metrics
- User management (view, tier, block/unblock)
- Session detail with interactive balance charts (TradingView)
- Live log streaming via WebSocket
- Audit log for all admin actions
- Dark mode toggle
- Telegram user deep-link auth

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /main-api/graphql/api/graphql` | All API calls (GraphQL) |

Base URL: `https://shuffle.com`

Auth header: `Authorization: Bearer <token>` (from browser DevTools)

---

## Files & Data

| Path | Purpose |
|---|---|
| `shuffle.py` | Main bot (single file) |
| `~/.shuffle_autobot.db` | SQLite database (sessions + bets) |
| `~/.shuffle_autobot.json` | Saved config for `--resume` |
| `~/.shuffle_presets.json` | Named strategy presets |
| `~/.shuffle_logs/shuffle.log` | Daily rotating log (30 days) |
| `~/.shuffle_autobot_live.json` | Live state for `--status` / `--monitor` |
| `~/.shuffle_autobot.pid` | PID file for `--stop` |

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.
