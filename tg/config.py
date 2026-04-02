"""Configuration — environment variables, constants, tier definitions."""

import os
import logging

# ── Environment ──────────────────────────────────────────
APP_ENV   = os.environ.get("APP_ENV", "production")
BOT_TOKEN = os.environ.get("SHUFFLE_TG_TOKEN", "")
DATA_DIR  = os.environ.get("SHUFFLE_TG_DATA", os.path.expanduser("~/.shufflebot_tg"))
WEB_URL   = os.environ.get("SHUFFLE_WEB_URL", "")          # e.g. http://IP:8001
WEB_SECRET = os.environ.get("SHUFFLE_WEB_SECRET_KEY", "")   # must match web app

# ── API ──────────────────────────────────────────────────
API_URL = "https://shuffle.com/main-api/graphql/api/graphql"

# Minimum bet on Shuffle
MIN_BET = 0.001

# ── Rate limit tiers: seconds between bets ───────────────
TIERS = {
    "free":  1.0,    # 1 bet/sec
    "trial": 0.0,    # max speed
}

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("shufflebot")

# Suppress noisy HTTP client logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Config keys persisted per user ───────────────────────
CONFIG_KEYS = [
    "bearer_token",
    "currency", "game",
    "multiplier_target", "dice_target", "dice_condition",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold",
]

# ── Supported currencies on Shuffle ──────────────────────
CURRENCIES = [
    "btc", "eth", "ltc", "doge", "trx", "bch", "xrp",
    "usdt", "usdc", "sol", "bnb",
]

# ── Available games ───────────────────────────────────────
GAME_LABELS = {
    "limbo": "Limbo",
    "dice":  "Dice",
}


def get_user_tier(user_id: int) -> str:
    """Return rate-limit tier for a user. Extensible for paid tiers."""
    return "trial"
