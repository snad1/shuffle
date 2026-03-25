#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
#  Shuffle AutoBot — Server Installer v1.0
#  Sets up on Linux servers with apt package manager
#  Sets up: venv, systemd, shufflectl
#  No Docker/FlareSolverr needed (Bearer token auth)
# ─────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$HOME/shuffle-bot"
SERVICE_NAME="shuffle"
TG_SERVICE_NAME="shuffle-tg"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SHUFFLECTL="$HOME/.local/bin/shufflectl"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Shuffle AutoBot — Server Installer v1.0          ║"
echo "║   CLI Bot + Telegram Bot                           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ─────────────────────────────
echo "[1/7] Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    sqlite3 tmux curl

# ── 2. Bot directory ───────────────────────────────────
echo "[2/7] Setting up bot directory…"
mkdir -p "$INSTALL_DIR"
cp shuffle.py      "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"
# Copy core module
mkdir -p "$INSTALL_DIR/core" "$INSTALL_DIR/tg"
cp core/__init__.py  "$INSTALL_DIR/core/"
cp core/strategy.py  "$INSTALL_DIR/core/"
cp core/database.py  "$INSTALL_DIR/core/"
cp core/engine.py    "$INSTALL_DIR/core/"
# Copy TG bot module
cp tg/__init__.py    "$INSTALL_DIR/tg/"
cp tg/config.py      "$INSTALL_DIR/tg/"
cp tg/database.py    "$INSTALL_DIR/tg/"
cp tg/engine.py      "$INSTALL_DIR/tg/"
cp tg/formatter.py   "$INSTALL_DIR/tg/"
cp tg/handlers.py    "$INSTALL_DIR/tg/"
cp tg/bot.py         "$INSTALL_DIR/tg/"
# Copy .env
if [ -f ".env" ]; then
    cp .env "$INSTALL_DIR/.env"
    echo "   Copied .env to $INSTALL_DIR/.env"
elif [ -f ".env.example" ]; then
    cp .env.example "$INSTALL_DIR/.env"
    echo "   Created $INSTALL_DIR/.env from .env.example"
fi

# ── 3. Python virtual environment ─────────────────────
echo "[3/7] Creating Python virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 4. Systemd user services ─────────────────────────
echo "[4/7] Setting up systemd services…"
mkdir -p "$SYSTEMD_DIR"

# CLI bot service
cat > "$SYSTEMD_DIR/${SERVICE_NAME}.service" << SERVICEEOF
[Unit]
Description=Shuffle AutoBot — Multi-Game Auto-Betting Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/shuffle.py --resume --daemon
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=15

# Environment
Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SERVICEEOF

# Telegram bot service
cat > "$SYSTEMD_DIR/${TG_SERVICE_NAME}.service" << TGSERVICEEOF
[Unit]
Description=Shuffle Telegram Bot — Multi-Game Auto-Betting via Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 -m tg.bot
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=15

# Environment
Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${INSTALL_DIR}
EnvironmentFile=-${HOME}/.shuffle_tg_env

[Install]
WantedBy=default.target
TGSERVICEEOF

sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true
systemctl --user daemon-reload
echo "   Services installed: ${SERVICE_NAME}.service, ${TG_SERVICE_NAME}.service"

# ── 5. Management scripts ─────────────────────────────
echo "[5/7] Creating management commands…"
mkdir -p "$HOME/.local/bin"

cp shufflectl "$SHUFFLECTL"
chmod +x "$SHUFFLECTL"

# ── 6. TG bot env file placeholder ─────────────────────
echo "[6/7] Checking Telegram bot config…"
if [ -f "$HOME/.shuffle_tg_env" ]; then
    echo "   Telegram bot token already configured."
else
    echo "   No TG token yet. Set with: shufflectl tg setup"
fi

# ── 7. Quick-run scripts ─────────────────────────────
echo "[7/7] Creating helper scripts…"

cat > "$INSTALL_DIR/run.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/venv/bin/python3" "$DIR/shuffle.py" "$@"
EOF
chmod +x "$INSTALL_DIR/run.sh"

cat > "$INSTALL_DIR/run-tg.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; source "$DIR/.env" 2>/dev/null; set +a
cd "$DIR" && "$DIR/venv/bin/python3" -m tg.bot "$@"
EOF
chmod +x "$INSTALL_DIR/run-tg.sh"

# ── Done ──────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════════════════"
echo ""
echo "  FIRST TIME SETUP:"
echo "    shufflectl setup          Configure tokens, strategy, etc."
echo ""
echo "  DAILY USE:"
echo "    shufflectl start          Start bot as background daemon"
echo "    shufflectl stop           Stop the bot"
echo "    shufflectl monitor        Attach live TUI to running daemon"
echo "    shufflectl status         Quick status snapshot"
echo "    shufflectl logs           Stream live logs"
echo "    shufflectl stats          View all-time statistics"
echo "    shufflectl session ID     Full stats + streak distribution"
echo ""
echo "  SHORTCUTS:"
echo "    shufflectl interactive    Start daemon + attach monitor"
echo "    shufflectl tmux           Monitor in detachable tmux session"
echo ""
echo "  TELEGRAM BOT:"
echo "    shufflectl tg setup       Set bot token"
echo "    shufflectl tg start       Start Telegram bot"
echo "    shufflectl tg stop        Stop Telegram bot"
echo "    shufflectl tg restart     Restart Telegram bot"
echo "    shufflectl tg status      Check Telegram bot status"
echo "    shufflectl tg logs        Stream Telegram bot logs"
echo ""
echo "  Both bots run as systemd user services — they will:"
echo "    - Auto-restart on crashes (after 10s)"
echo "    - Keep running after you log out (lingering enabled)"
echo "    - Log to journalctl (shufflectl logs)"
echo ""
echo "  FILES:"
echo "    Bot:        $INSTALL_DIR/"
echo "    Config:     ~/.shuffle_autobot.json"
echo "    Database:   ~/.shuffle_autobot.db"
echo "    Logs:       ~/.shuffle_logs/shuffle.log"
echo "    Service:    $SYSTEMD_DIR/${SERVICE_NAME}.service"
echo ""
