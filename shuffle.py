#!/usr/bin/env python3
"""
+----------------------------------------------------------+
|   Shuffle AutoBot v1.0  -- Multi-Game Auto-Betting Engine |
|   Limbo + Dice | Live TUI | Session logging & strategies  |
+----------------------------------------------------------+
"""

import os, sys, json, time, sqlite3, threading, signal, random, string, argparse, logging, re, queue
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List
from logging.handlers import TimedRotatingFileHandler

from shared.core.session_rules import (
    RECURRING_DELAY_MAX, clamp, parse_streak_delay, record_outcome,
    restart_delay_for, should_restart, streak_delay_for, streak_reset_stop_reason,
    streak_reset_triggered, streakbet_loss_factor, martingale_chain_depth,
)
from shared.core.stats import stats_date_window, fmt_duration, sessions_time_span
from shared.core.database import ensure_sessions_lane, cleanup_old_bets, cleanup_live_bets
from shared.core.diagnostics import percentile, benchmark_verdict
from shared.core.net import validate_proxy, mask_proxy
from shared.core.lanes import (
    LANES_MAX, LANE_SPLIT_HELP, LANE_UNSET_VALUES, check_lane_keys, lane_budget_exceeded,
    lane_config, lane_equity, lane_path, lane_share, lane_split, lanes_count,
    recommend_lanes, split_base_bet, split_budget_on, split_lane_pairs, validate_lane,
)
try:
    import readline
except ImportError:
    pass

try:
    import requests
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.align import Align
    from rich.rule import Rule
    from rich import box
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich")
    sys.exit(1)

# Try curl_cffi for TLS fingerprint bypass on servers; fall back to requests
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

def _create_http():
    if _HAS_CFFI:
        sess = cffi_requests.Session(impersonate="chrome")
    else:
        sess = requests.Session()
    _apply_proxy_to_session(sess)
    return sess


def _apply_proxy_to_session(sess=None) -> None:
    """v1.9.0 — route the HTTP session through state.proxy (WolfBet v2.26.0).

    Both curl_cffi and requests sessions honour `.proxies`. `_create_http`
    calls this, so the client recreated on a connection reset keeps the proxy.
    """
    sess = sess if sess is not None else globals().get("_http")
    if sess is None:
        return
    st = globals().get("state")
    p = getattr(st, "proxy", None)
    sess.proxies = {"http": p, "https": p} if p else {}

_http = _create_http()

# ===========================================================
#  CONSTANTS
# ===========================================================
API_URL      = "https://shuffle.com/main-api/graphql/api/graphql"
DB_PATH      = os.path.expanduser("~/.shuffle_autobot.db")
CONFIG_PATH  = os.path.expanduser("~/.shuffle_autobot.json")
STATE_PATH   = os.path.expanduser("~/.shuffle_autobot_live.json")
PID_PATH     = os.path.expanduser("~/.shuffle_autobot.pid")
PRESET_PATH  = os.path.expanduser("~/.shuffle_presets.json")
LOG_DIR      = os.path.expanduser("~/.shuffle_logs")
VERSION      = "1.11.0"
MIN_BET      = 0.001   # Shuffle minimum bet
APP_ENV      = os.environ.get("APP_ENV", "production")

# -- Daily rotating logger --
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("shuffle")
logger.setLevel(logging.WARNING)
_log_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "shuffle.log"),
    when="midnight", backupCount=30, utc=False,
)
_log_handler.suffix = "%Y-%m-%d"
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_log_handler)

# v1.10.0 — parallel lanes (WolfBet v2.35.0). Lane 1 keeps these paths; lanes
# 2..N get `.laneK` state / pid files. Config file, DB and log file are shared.
_BASE_STATE_PATH, _BASE_PID_PATH = STATE_PATH, PID_PATH

# Keys that can't differ per lane: one account, one client setup, one DB.
_LANE_FORBIDDEN_KEYS = frozenset({
    "bearer_token", "currency", "game", "proxy", "purge_days", "db_write_mode",
})

CURRENCIES = ["btc", "eth", "ltc", "doge", "trx", "bch", "xrp", "usdt", "bnb", "ada", "matic", "shib"]

STRATEGIES = {
    "1": ("Flat Bet",          "Same amount every single bet -- low risk, low variance"),
    "2": ("Martingale",        "Double bet on loss, reset to base on win -- classic high-risk"),
    "3": ("Anti-Martingale",   "Double bet on win, reset to base on loss -- ride hot streaks"),
    "4": ("D'Alembert",        "Add 1 unit on loss, remove 1 unit on win -- gentler progression"),
    "5": ("Paroli (3-step)",   "Double on win up to 3x then reset -- safer streak play"),
    "6": ("Delay Martingale",  "Flat for N losses then double -- delays escalation"),
    "7": ("Rule-Based",        "Build custom conditions & actions -- full control"),
}

# ===========================================================
#  GAME REGISTRY  (GraphQL API)
# ===========================================================
# Each game defines:
#   build_query(state) -> (query_str, variables_dict)
#   parse_result(data) -> dict with keys:
#       amount, payout, payout_mult, result_display, is_win, raw_state

GAMES = {}

def _register_game(name, label, build_query, parse_result, operation_name, response_key):
    GAMES[name] = {
        "label": label,
        "build_query": build_query,
        "parse_result": parse_result,
        "operation_name": operation_name,
        "response_key": response_key,
    }

def _gen_window_id() -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=10))


# -- LIMBO --
_LIMBO_QUERY = """mutation LimboPlay($data: LimboPlayInput!) {
  limboPlay(data: $data) {
    id
    currency
    amount
    payout
    afterBalance
    shuffleOriginalActions {
      id
      action {
        limbo {
          resultRaw
          resultValue
          userValue
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}"""

def _limbo_build(st):
    return _LIMBO_QUERY, {
        "data": {
            "amount": f"{st.current_bet:.8f}",
            "bet": f"{st.multiplier_target:.2f}",
            "currency": st.currency.upper(),
            "windowId": _gen_window_id(),
            "usdAmount": 0,
        }
    }

def _limbo_parse(data):
    bet_data = data.get("limboPlay", data)
    amount = float(bet_data.get("amount", 0))
    payout = float(bet_data.get("payout", 0))
    after_balance = float(bet_data.get("afterBalance", 0))
    actions = bet_data.get("shuffleOriginalActions", [])
    result_value = 0.0
    user_value = 0.0
    if actions:
        limbo_action = actions[0].get("action", {}).get("limbo", {})
        result_value = float(limbo_action.get("resultValue", 0))
        user_value = float(limbo_action.get("userValue", 0))
    is_win = result_value >= user_value if user_value > 0 else payout > 0
    payout_mult = result_value if is_win else 0.0
    return {
        "amount": amount,
        "payout": payout,
        "payout_mult": payout_mult,
        "result_display": f"{result_value:.4f}x",
        "result_value": result_value,
        "is_win": is_win,
        "after_balance": after_balance,
        "raw_state": bet_data,
    }

_register_game("limbo", "Limbo", _limbo_build, _limbo_parse, "LimboPlay", "limboPlay")


# -- DICE --
_DICE_QUERY = """mutation DicePlay($data: DicePlayInput!) {
  dicePlay(data: $data) {
    id
    currency
    amount
    payout
    afterBalance
    shuffleOriginalActions {
      id
      action {
        dice {
          ...DiceFields
          __typename
        }
        __typename
      }
      __typename
    }
    updatedAt
    createdAt
    __typename
  }
}

fragment DiceFields on DiceActionModel {
  userDiceDirection
  userValue
  resultValue
  resultRaw
  __typename
}"""

def _dice_build(st):
    return _DICE_QUERY, {
        "data": {
            "amount": f"{st.current_bet:.8f}",
            "bet": f"{st.dice_target:.2f}",
            "currency": st.currency.upper(),
            "direction": st.dice_condition.upper(),
            "usdAmount": 0,
        }
    }

def _dice_parse(data):
    bet_data = data.get("dicePlay", data)
    amount = float(bet_data.get("amount", 0))
    payout = float(bet_data.get("payout", 0))
    after_balance = float(bet_data.get("afterBalance", 0))
    actions = bet_data.get("shuffleOriginalActions", [])
    result_value = 0.0
    user_value = ""
    direction = ""
    if actions:
        dice_action = actions[0].get("action", {}).get("dice", {})
        result_value = float(dice_action.get("resultValue", 0))
        user_value = dice_action.get("userValue", "")
        direction = dice_action.get("userDiceDirection", "")
    if direction and user_value:
        uv = float(user_value)
        is_win = result_value > uv if direction == "ABOVE" else result_value < uv
    else:
        is_win = payout > 0
    payout_mult = 0.0
    if is_win and amount > 0:
        payout_mult = payout / amount
    return {
        "amount": amount,
        "payout": payout,
        "payout_mult": payout_mult,
        "result_display": f"{result_value:.2f}",
        "result_value": result_value,
        "is_win": is_win,
        "after_balance": after_balance,
        "raw_state": bet_data,
    }

_register_game("dice", "Dice", _dice_build, _dice_parse, "DicePlay", "dicePlay")


# ===========================================================
#  DATABASE SETUP
# ===========================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT,
            ended_at      TEXT,
            currency      TEXT,
            game          TEXT,
            strategy      TEXT,
            base_bet      REAL,
            multiplier    REAL,
            total_bets    INTEGER DEFAULT 0,
            wins          INTEGER DEFAULT 0,
            losses        INTEGER DEFAULT 0,
            profit        REAL    DEFAULT 0,
            wagered       REAL    DEFAULT 0,
            start_balance REAL,
            end_balance   REAL,
            max_win_streak  INTEGER DEFAULT 0,
            max_loss_streak INTEGER DEFAULT 0,
            highest_balance REAL DEFAULT 0,
            lowest_balance  REAL DEFAULT 0,
            highest_win     REAL DEFAULT 0,
            biggest_loss    REAL DEFAULT 0,
            bets_per_minute REAL DEFAULT 0,
            bets_per_second REAL DEFAULT 0,
            peak_bps        REAL DEFAULT 0,
            low_bps         REAL DEFAULT 0,
            peak_bpm        REAL DEFAULT 0,
            low_bpm         REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER,
            timestamp    TEXT,
            game         TEXT,
            amount       REAL,
            multiplier_target REAL,
            result_value REAL,
            result_display TEXT,
            state        TEXT,
            profit       REAL,
            balance_after REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    for col, defn in [
        ("game", "TEXT DEFAULT 'limbo'"),
        ("result_display", "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f"ALTER TABLE bets ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    ensure_sessions_lane(c)   # v1.8.0 — sessions.lane (WolfBet v2.35.0); old rows = lane 1
    conn.commit()
    conn.close()

# ===========================================================
#  CONFIG PERSISTENCE
# ===========================================================
_CONFIG_KEYS = [
    "bearer_token", "currency", "game",
    "multiplier_target", "dice_target", "dice_condition",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold",
    # v1.7.0 — WolfBet v2.16.0 / v2.25.3 / v2.28.2 / v2.32.0 session keys.
    "recurring", "recurring_delay", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "streak_reset_losses", "streak_reset_delay",
    # v1.9.0 — WolfBet v2.26.0 / v2.27.0 tooling keys.
    "proxy", "purge_days", "db_write_mode",
]

# (N, X) tuples, stored on disk as "N:X" strings (null when off), WolfBet v2.28.2.
_STREAK_TUPLE_KEYS = ["streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss"]

# Delays clamped on load / reload: (attr, label). CLI floor is 0.
_CLAMPED_DELAYS = [("recurring_delay", "recurring_delay"),
                   ("streak_reset_delay", "streak_reset_delay")]


def _encode_streak_tuple(tup):
    if not tup or not tup[0]:
        return None
    return f"{int(tup[0])}:{float(tup[1]):g}"


def _serialize_keys(keys) -> dict:
    data = {k: getattr(state, k) for k in keys}
    for k in _STREAK_TUPLE_KEYS:
        data[k] = _encode_streak_tuple(getattr(state, k))
    return data


def _clamp_delays() -> list:
    """Clamp the pause delays into 0..RECURRING_DELAY_MAX; return the messages."""
    msgs = []
    for attr, label in _CLAMPED_DELAYS:
        try:
            val = float(getattr(state, attr))
        except (TypeError, ValueError):
            val = 0.0
        val, msg = clamp(label, val, 0, RECURRING_DELAY_MAX)
        setattr(state, attr, val)
        if msg:
            msgs.append(msg)
            logger.warning(msg)
    return msgs


def _sanitize_tooling_keys() -> None:
    """Validate proxy / purge_days / db_write_mode after a load or reload; apply the proxy."""
    try:
        state.proxy = validate_proxy(state.proxy)
    except ValueError as e:
        logger.warning("config proxy ignored: %s", e)
        console.print(f"[yellow]Saved proxy ignored: {e}[/]")
        state.proxy = None
    try:
        state.purge_days = int(max(1, min(30, int(state.purge_days))))
    except (TypeError, ValueError):
        state.purge_days = 3
    if str(state.db_write_mode).strip().lower() not in ("async", "sync"):
        state.db_write_mode = "async"
    _apply_proxy_to_session()


def _apply_loaded(data: dict, keys) -> None:
    """Apply a saved config / preset dict to `state` and re-arm per-session values."""
    for k in keys:
        if k in data:
            setattr(state, k, data[k])
    for k in _STREAK_TUPLE_KEYS:
        if k in data:
            setattr(state, k, parse_streak_delay(data[k]))
    for msg in _clamp_delays():
        console.print(f"[yellow]{msg}[/]")
    _sanitize_tooling_keys()
    state.current_bet = state.base_bet
    if state.profit_threshold:
        state.next_profit_milestone = state.profit_threshold
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)

def _mask_key(key: str) -> str:
    """Mask token/key for safe logging: show first 8 + last 4 chars."""
    if not key or len(key) < 16:
        return "***"
    return f"{key[:8]}...{key[-4:]}"

def _write_config_raw(data: dict) -> None:
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def save_config():
    # v1.10.0 — the base config is lane 1's; lanes 2..N never write it.
    if state.lane > 1:
        logger.info("save_config skipped on lane %d (base config is lane 1's)", state.lane)
        return
    data = _serialize_keys(_CONFIG_KEYS)
    # An untouched split bet is saved as the full bet, so a reload doesn't split it twice.
    if state.base_bet_split is not None and state.base_bet == state.base_bet_split:
        data["base_bet"] = state.base_bet_disk
    try:
        with open(CONFIG_PATH) as f:
            old = json.load(f)
        if isinstance(old, dict) and "lanes" in old:
            data["lanes"] = old["lanes"]
    except (OSError, ValueError):
        pass
    _write_config_raw(data)
    logger.info("Config saved (token=%s)", _mask_key(data.get("bearer_token", "")))

def load_config() -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        # v1.10.0 — lane overrides and the lane bet split go over the base config.
        state.lanes_count = lanes_count(data)
        state.lane_split = lane_split(data)
        # v1.11.0 (WolfBet v2.36.0) — a split bet below MIN_BET stays full.
        merged, warn = lane_config(data, state.lane, _LANE_FORBIDDEN_KEYS, MIN_BET)
        if warn:
            logger.warning(warn)
        state.base_bet_disk = data.get("base_bet")
        mb = merged.get("base_bet")
        state.base_bet_split = mb if mb != state.base_bet_disk else None
        _apply_loaded(merged, _CONFIG_KEYS)
        return True
    except Exception:
        return False

# ===========================================================
#  PRESETS
# ===========================================================
_PRESET_KEYS = [
    "currency", "game", "multiplier_target", "dice_target", "dice_condition",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold",
    "recurring", "recurring_delay", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "streak_reset_losses", "streak_reset_delay",
    "proxy", "purge_days", "db_write_mode",
]

# v1.7.0 — fields re-read into a running bot on SIGHUP (WolfBet v2.28.3) without
# touching the session identity, counters or the Martingale ladder. Anything else
# needs a restart.
_LIVE_RELOAD_KEYS = {
    "recurring", "recurring_delay", "streak_reset_losses", "streak_reset_delay",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "bet_delay", "basebet_pct",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold",
    "custom_rules_text",
    # v1.9.0 — proxy is re-applied to the live HTTP session; db_write_mode only
    # takes effect for the writer thread on the next start.
    "proxy", "purge_days", "db_write_mode",
}


def _reload_live_config() -> dict:
    """Re-read CONFIG_PATH and apply only live-safe fields; return {field: (old, new)}."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("live reload: failed to parse config: %s", e)
        return {}
    # v1.10.0 — merge this lane's overrides and bet split (WolfBet v2.35.0 / v2.36.0).
    # The lane count and split mode themselves apply on the next start.
    data, _ = lane_config(data, state.lane, _LANE_FORBIDDEN_KEYS, MIN_BET)
    applied, skipped = {}, []
    tuple_keys = set(_STREAK_TUPLE_KEYS)
    with state.lock:
        for k, v in data.items():
            new_val = parse_streak_delay(v) if k in tuple_keys else v
            old_val = getattr(state, k, None)
            if k in _LIVE_RELOAD_KEYS:
                if old_val != new_val:
                    setattr(state, k, new_val)
                    applied[k] = (old_val, new_val)
            elif k in _CONFIG_KEYS and old_val != new_val:
                skipped.append(k)
        _clamp_delays()
    if {"proxy", "purge_days", "db_write_mode"} & set(applied):
        _sanitize_tooling_keys()
    if "custom_rules_text" in applied and state.custom_rules_text:
        try:
            state.custom_rules = _load_rules_from_text(state.custom_rules_text)
        except Exception as e:
            logger.warning("live reload: rule reparse failed: %s", e)
    if skipped:
        logger.warning("live reload: skipped %d unsafe field(s) — restart to apply: %s",
                       len(skipped), ", ".join(sorted(skipped)))
    return applied


def _sig_reload(sig, frame):
    """SIGHUP handler: reload live-safe config. Never raises."""
    try:
        applied = _reload_live_config()
        if applied:
            logger.info("SIGHUP: reloaded %d live field(s): %s",
                        len(applied), ", ".join(sorted(applied)))
        else:
            logger.info("SIGHUP: no live-safe changes to apply")
    except Exception as e:
        logger.warning("SIGHUP handler raised: %s", e)

def _load_all_presets() -> dict:
    if not os.path.exists(PRESET_PATH):
        return {}
    try:
        with open(PRESET_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_all_presets(presets: dict):
    fd = os.open(PRESET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(presets, f, indent=2)

def save_preset(name: str):
    presets = _load_all_presets()
    presets[name] = _serialize_keys(_PRESET_KEYS)
    _save_all_presets(presets)

def load_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    _apply_loaded(presets[name], _PRESET_KEYS)
    return True

def delete_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    del presets[name]
    _save_all_presets(presets)
    return True

def list_presets() -> dict:
    return _load_all_presets()

# ===========================================================
#  RULE-BASED STRATEGY ENGINE
# ===========================================================

COND_TYPES = {
    "1": "sequence",
    "2": "profit",
    "3": "bet",
}

SEQ_MODES = {
    "1": ("every",         "Every N (total count)"),
    "2": ("every_streak",  "Every N in streak (N, 2N, 3N...)"),
    "3": ("first_streak",  "First streak of N (once, resets on flip)"),
    "4": ("streak_above",  "Streak above N (every bet past N)"),
    "5": ("streak_below",  "Streak below N"),
}
SEQ_TRIGGERS = {"1": "win", "2": "loss", "3": "bet"}

PROFIT_FIELDS = {"1": "profit", "2": "loss", "3": "balance"}

BET_FIELDS = {"1": "amount", "2": "number", "3": "winchance", "4": "payout"}

CMP_OPS = {
    "1": ("gte", ">="),
    "2": ("gt",  ">"),
    "3": ("lte", "<="),
    "4": ("lt",  "<"),
}

RULE_ACTIONS = {
    "1":  ("reset_amount",    "Reset bet amount"),
    "2":  ("increase_amount", "Increase amount by %"),
    "3":  ("decrease_amount", "Decrease amount by %"),
    "4":  ("add_amount",      "Add to amount"),
    "5":  ("deduct_amount",   "Deduct from amount"),
    "6":  ("set_amount",      "Set amount"),
    "7":  ("switch",          "Switch above/below (dice only)"),
    "8":  ("stop",            "Stop betting"),
    "9":  ("set_winchance",   "Set win chance (changes multiplier)"),
    "10": ("increase_wc",     "Increase win chance by %"),
    "11": ("decrease_wc",     "Decrease win chance by %"),
    "12": ("reset_game",      "Reset game (full reset)"),
}


class StrategyRule:
    __slots__ = ("cond_type", "cond_field", "cond_mode", "cond_value",
                 "cond_trigger", "action", "action_value", "description")

    def __init__(self, cond_type="", cond_field="", cond_mode="", cond_value=0.0,
                 cond_trigger="", action="", action_value=0.0, description=""):
        self.cond_type    = cond_type
        self.cond_field   = cond_field
        self.cond_mode    = cond_mode
        self.cond_value   = cond_value
        self.cond_trigger = cond_trigger
        self.action       = action
        self.action_value = action_value
        self.description  = description

    def __repr__(self):
        return f"Rule({self.description})"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyRule":
        return cls(**{k: d.get(k, "") for k in cls.__slots__})


def _describe_rule(r: StrategyRule) -> str:
    if r.cond_type == "sequence":
        mode_label = dict(every="Every", every_streak="Every streak of",
                          first_streak="First streak of",
                          streak_above="Streak above", streak_below="Streak below").get(r.cond_mode, r.cond_mode)
        cond = f"{mode_label} {int(r.cond_value)} {r.cond_trigger}(s)"
    elif r.cond_type == "profit":
        op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
        cond = f"On {r.cond_field} {op_sym} {r.cond_value}"
    elif r.cond_type == "bet":
        op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
        cond = f"On bet {r.cond_field} {op_sym} {r.cond_value}"
    else:
        cond = "?"

    act_labels = {k: v[1] for k, v in RULE_ACTIONS.items()}
    act_name = act_labels.get(
        next((k for k, v in RULE_ACTIONS.items() if v[0] == r.action), ""), r.action
    )
    if r.action_value and "%" in act_name:
        act = f"{act_name.replace(' %', '')} {r.action_value}%"
    elif r.action_value:
        act = f"{act_name} {r.action_value}"
    else:
        act = act_name

    return f"{cond} -> {act}"


def _cmp(actual: float, mode: str, threshold: float) -> bool:
    if mode == "gte": return actual >= threshold
    if mode == "gt":  return actual > threshold
    if mode == "lte": return actual <= threshold
    if mode == "lt":  return actual < threshold
    return False


def _get_win_chance() -> float:
    """Calculate current win chance based on game type."""
    if state.game == "dice":
        if state.dice_condition == "above":
            return 100.0 - state.dice_target
        else:
            return state.dice_target
    else:  # limbo
        return 99.0 / state.multiplier_target if state.multiplier_target > 0 else 0


def _apply_action(rule: StrategyRule):
    a = rule.action
    v = rule.action_value

    if a == "reset_amount":
        state.current_bet = state.base_bet
    elif a == "increase_amount":
        state.current_bet *= (1 + v / 100)
    elif a == "decrease_amount":
        state.current_bet *= (1 - v / 100)
        state.current_bet = max(state.current_bet, state.base_bet)
    elif a == "add_amount":
        state.current_bet += v
    elif a == "deduct_amount":
        state.current_bet = max(state.base_bet, state.current_bet - v)
    elif a == "set_amount":
        state.current_bet = max(state.base_bet, v)
    elif a == "switch":
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_condition = "below"
                state.dice_target = round(99.0 / state.multiplier_target, 2)
            else:
                state.dice_condition = "above"
                state.dice_target = round(100.0 - 99.0 / state.multiplier_target, 2)
            logger.debug("RULE SWITCH -> %s  target=%s", state.dice_condition, state.dice_target)
    elif a == "stop":
        state.running = False
        state.status = f"Rule stop: {rule.description}"
        logger.warning("RULE STOP triggered: %s", rule.description)
    elif a == "set_winchance":
        wc = max(0.01, min(98.99, v))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "increase_wc":
        wc = _get_win_chance()
        wc = min(98.99, wc * (1 + v / 100))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "decrease_wc":
        wc = _get_win_chance()
        wc = max(0.01, wc * (1 - v / 100))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "reset_winchance":
        state.multiplier_target = state.initial_multiplier
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "add_wc":
        wc = _get_win_chance()
        wc = max(0.01, min(98.99, wc + v))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "deduct_wc":
        wc = _get_win_chance()
        wc = max(0.01, min(98.99, wc - v))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "reset_payout":
        state.multiplier_target = state.initial_multiplier
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "set_payout":
        state.multiplier_target = max(1.01, v)
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "increase_payout":
        state.multiplier_target = state.multiplier_target * (1 + v / 100)
        if state.game == "dice":
            wc = max(0.01, 99.0 / state.multiplier_target)
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "decrease_payout":
        state.multiplier_target = max(1.01, state.multiplier_target * (1 - v / 100))
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "add_payout":
        state.multiplier_target = state.multiplier_target + v
        if state.game == "dice":
            wc = max(0.01, 99.0 / state.multiplier_target)
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "deduct_payout":
        state.multiplier_target = max(1.01, state.multiplier_target - v)
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "reset_game":
        state.current_bet = state.base_bet
        state.current_streak = 0
        logger.info("RULE RESET GAME triggered: %s", rule.description)


def apply_rules(bet_state: str):
    for rule in state.custom_rules:
        triggered = False

        if rule.cond_type == "sequence":
            streak = state.current_streak
            trig = rule.cond_trigger
            n = rule.cond_value

            if rule.cond_mode == "every":
                if trig == "win" and bet_state == "win":
                    triggered = (state.wins > 0 and state.wins % int(n) == 0)
                elif trig == "loss" and bet_state == "loss":
                    triggered = (state.losses > 0 and state.losses % int(n) == 0)
                elif trig == "bet":
                    triggered = (state.total_bets > 0 and state.total_bets % int(n) == 0)
            elif rule.cond_mode == "every_streak":
                if trig == "win" and bet_state == "win":
                    triggered = (streak > 0 and streak % int(n) == 0)
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) > 0 and abs(streak) % int(n) == 0)
            elif rule.cond_mode == "first_streak":
                if trig == "win" and bet_state == "win":
                    triggered = (streak == int(n))
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) == int(n))
            elif rule.cond_mode == "streak_above":
                if trig == "win" and bet_state == "win":
                    triggered = (streak > int(n))
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) > int(n))
            elif rule.cond_mode == "streak_below":
                if trig == "win" and bet_state == "win":
                    triggered = (streak < int(n))
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) < int(n))

        elif rule.cond_type == "profit":
            field = rule.cond_field
            if field == "profit" and state.profit > 0:
                triggered = _cmp(state.profit, rule.cond_mode, rule.cond_value)
            elif field == "loss" and state.profit < 0:
                triggered = _cmp(abs(state.profit), rule.cond_mode, rule.cond_value)
            elif field == "balance":
                triggered = _cmp(state.current_balance, rule.cond_mode, rule.cond_value)

        elif rule.cond_type == "bet":
            field = rule.cond_field
            if field == "amount":
                triggered = _cmp(state.current_bet, rule.cond_mode, rule.cond_value)
            elif field == "number":
                triggered = _cmp(state.total_bets, rule.cond_mode, rule.cond_value)
            elif field == "winchance":
                wc = _get_win_chance()
                triggered = _cmp(wc, rule.cond_mode, rule.cond_value)
            elif field == "payout":
                triggered = _cmp(state.multiplier_target, rule.cond_mode, rule.cond_value)

        if triggered:
            logger.debug("RULE FIRED: %s", rule.description)
            _apply_action(rule)


def _load_rules_from_text(text: str) -> List[StrategyRule]:
    text = text.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            data = json.loads(text)
            rules = []
            for d in data:
                r = StrategyRule.from_dict(d)
                if not r.description:
                    r.description = _describe_rule(r)
                rules.append(r)
            return rules
        except (json.JSONDecodeError, TypeError):
            pass

    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(
            r'onEvery(\d+)(win|lose|bet)(Reset|Stop|Increase|Switch)\s*(.*)',
            line, re.IGNORECASE
        )
        if not m:
            logger.warning("Could not parse legacy rule: %s", line)
            continue
        n       = int(m.group(1))
        trigger = m.group(2).lower()
        action  = m.group(3).lower()
        params  = m.group(4).strip()
        value   = 0.0

        if action == "increase":
            pct = re.search(r'(\d+(?:\.\d+)?)%', params)
            if pct:
                value = float(pct.group(1))

        action_map = {"reset": "reset_amount", "stop": "stop",
                      "increase": "increase_amount", "switch": "switch"}
        r = StrategyRule(
            cond_type="sequence", cond_mode="every", cond_value=float(n),
            cond_trigger=trigger if trigger != "lose" else "loss",
            action=action_map.get(action, action), action_value=value,
        )
        r.description = _describe_rule(r)
        rules.append(r)

    return rules


# ===========================================================
#  BOT STATE
# ===========================================================
class BotState:
    def __init__(self):
        self.lock = threading.Lock()

        # -- runtime flags --
        self.running  = False
        self.paused   = False
        self.stopping = False

        # -- config --
        self.bearer_token     = ""
        self.currency         = "usdt"
        self.game             = "limbo"
        self.multiplier_target = 2.0
        self.initial_multiplier = 2.0
        # dice-specific
        self.dice_target      = 50.5
        self.dice_condition   = "above"

        self.base_bet         = 0.0
        self.current_bet      = 0.0
        self.strategy         = "Martingale"
        self.strategy_key     = "2"
        self.win_mult         = 1.0
        self.loss_mult        = 2.0

        # -- rule-based strategy --
        self.custom_rules: List[StrategyRule] = []
        self.custom_rules_text: str = ""

        # -- stop conditions --
        self.max_profit      : Optional[float] = None
        self.max_loss        : Optional[float] = None
        self.max_bets        : Optional[int]   = None
        self.max_wins        : Optional[int]   = None
        self.stop_on_balance : Optional[float] = None

        # -- session info --
        self.session_id     = None
        self.session_start  = time.time()
        self.total_bets     = 0
        self.wins           = 0
        self.losses         = 0
        self.profit         = 0.0
        self.wagered        = 0.0
        self.start_balance  = 0.0
        self.current_balance= 0.0

        # -- streaks --
        self.current_streak  = 0
        self.max_win_streak  = 0
        self.max_loss_streak = 0

        # -- extremes --
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")

        # -- bet pacing --
        self.bet_delay       = 0
        self.backoff_delay   = 1.0
        self.max_backoff     = 30.0
        self.consecutive_errors = 0
        self._insufficient_balance = False

        # -- performance --
        self.bets_per_minute = 0.0
        self.bets_per_second = 0.0
        self.peak_bps        = 0.0
        self.low_bps         = float("inf")
        self.peak_bpm        = 0.0
        self.low_bpm         = float("inf")
        self._bets_this_sec  = 0
        self._bets_this_min  = 0
        self._current_sec    = 0
        self._current_min    = 0
        self.bet_timestamps  = deque(maxlen=120)

        # -- profit history for sparkline --
        self.profit_history  = deque(maxlen=80)
        self.profit_history.append(0.0)

        # -- recent bets --
        self.recent_bets = deque(maxlen=10)

        # -- status messages --
        self.status     = "Initializing..."
        self.last_error = ""

        # -- profit-based base bet increment --
        self.profit_increment    : Optional[float] = None
        self.profit_threshold    : Optional[float] = None
        self.next_profit_milestone: float = 0.0

        # -- strategy internal state --
        self.dalembert_unit = 0
        self.paroli_count   = 0
        self.delay_martin_threshold = 3

        # -- v1.7.0 session cycles (WolfBet v2.16.0 / v2.32.0) --
        self.recurring          = False
        self.recurring_delay    : float = 60
        self.streak_reset_losses: int   = 0      # 0 = off
        self.streak_reset_delay : float = 600
        self.last_broken_loss_streak = 0
        # streak pacing / sizing, (N, X) tuples; N = 0 disables
        self.streak_delay_loss  = (0, 0.0)
        self.streak_delay_win   = (0, 0.0)
        self.streak_delay_bets  = (0, 0.0)
        self.streakbet_loss     = (0, 0.0)
        self.basebet_pct        : float = 0.0    # 0 = off; fraction of balance
        # milestone notifications (log + status only)
        self.milestone_bets     : int   = 100
        self.milestone_wins     : int   = 0
        self.milestone_losses   : int   = 0
        self.milestone_profit   : float = 0.0
        self._last_profit_milestone = 0.0
        # uptime freeze during the pause between cycles (WolfBet v2.28.4)
        self.session_bet_ended_at    : Optional[float] = None
        self.session_pause_started_at: Optional[float] = None
        self.session_pause_total     : float = 0.0
        self.run_started_at = time.time()

        # -- v1.9.0 observability (WolfBet v2.17.0 / v2.30.3-v2.30.5) --
        self._last_api_ms      = 0.0
        self._api_ms_total     = 0.0
        self._api_ms_count     = 0
        self.peak_api_ms       = 0.0
        self._api_samples      = deque(maxlen=500)
        self._on_target_count  = 0
        self._cycle_ms         = 0.0   # alias of cycle p50 (state-file compat)
        self._cycle_overhead_ms = 0.0
        self._cycle_samples    = deque(maxlen=500)

        # -- v1.9.0 tooling (WolfBet v2.26.0 / v2.27.0) --
        self.proxy        : Optional[str] = None
        self.purge_days   : int = 3
        self.db_write_mode: str = "async"

        # -- v1.10.0 parallel lanes (WolfBet v2.35.0 / v2.36.0) --
        self.lane            : int = 1
        self.lanes_count     : int = 1
        self.lane_split      : str = "none"
        self.lane_share      : Optional[float] = None   # None = no lane budget
        self.lane_run_profit : float = 0.0               # closed cycles of this run
        self.base_bet_disk   : Optional[float] = None    # base_bet as saved on disk
        self.base_bet_split  : Optional[float] = None    # base_bet after the lane bet split


state   = BotState()
console = Console()


# ===========================================================
#  LANES  (v1.10.0 — WolfBet v2.35.0 / v2.36.0)
# ===========================================================
def _apply_lane(lane) -> None:
    """Select lane K before any command: state / pid paths and the `[LK]` log prefix.

    Lane 1 keeps the classic paths and log format.
    """
    global STATE_PATH, PID_PATH
    lane = validate_lane(lane)
    state.lane = lane
    if lane > 1:
        STATE_PATH = lane_path(_BASE_STATE_PATH, lane)
        PID_PATH = lane_path(_BASE_PID_PATH, lane)
        _log_handler.setFormatter(logging.Formatter(
            f"%(asctime)s  %(levelname)-7s  [L{lane}] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))


def _lane_equity():
    """Money this lane may still risk (share + its result), None without a budget split."""
    return lane_equity(state.lane_share, state.lane_run_profit, state.profit)


def _init_lane_budget() -> None:
    """Set this lane's share of the balance at bot start (`budget` / `both` split only)."""
    state.lane_share = lane_share(state.current_balance, state.lanes_count, state.lane_split)
    if state.lane_share is not None:
        logger.warning("LANE %d/%d share=%.8f %s (split %s)", state.lane, state.lanes_count,
                       state.lane_share, state.currency.upper(), state.lane_split)


# ===========================================================
#  METRICS  (v1.9.0 — WolfBet v2.17.0 / v2.30.3-v2.30.5)
# ===========================================================
def _compute_api_avg() -> float:
    """All-time mean API time in ms (0 when no samples yet)."""
    if state._api_ms_count == 0:
        return 0.0
    return state._api_ms_total / state._api_ms_count


def _compute_api_p50() -> float:
    """Median API time in ms over the last-500-sample window (0 when empty)."""
    if not state._api_samples:
        return 0.0
    return percentile(sorted(state._api_samples), 0.50)


def _compute_api_p99() -> float:
    """99th-percentile API time in ms over the last-500-sample window (0 when empty)."""
    if not state._api_samples:
        return 0.0
    return percentile(sorted(state._api_samples), 0.99)


def _compute_on_target_pct() -> Optional[float]:
    """% of bets whose API time <= bet_delay; None when no delay or no samples."""
    if state.bet_delay <= 0 or state._api_ms_count == 0:
        return None
    return (state._on_target_count / state._api_ms_count) * 100


def _compute_api_rolling_avg() -> float:
    """Mean API ms over the same window as p50 / p99 (0 when empty)."""
    if not state._api_samples:
        return 0.0
    return sum(state._api_samples) / len(state._api_samples)


def _compute_cycle_p50() -> float:
    """Median per-bet cycle time in ms over the rolling window (0 when empty)."""
    if not state._cycle_samples:
        return 0.0
    return percentile(sorted(state._cycle_samples), 0.50)


def _compute_cycle_p99() -> float:
    """99th-percentile per-bet cycle time in ms (0 when empty)."""
    if not state._cycle_samples:
        return 0.0
    return percentile(sorted(state._cycle_samples), 0.99)


def _compute_cycle_mean() -> float:
    """Mean per-bet cycle time in ms (0 when empty); drives recent BPS."""
    if not state._cycle_samples:
        return 0.0
    return sum(state._cycle_samples) / len(state._cycle_samples)


def _compute_recent_bps() -> float:
    """Windowed BPS = 1000 / cycle mean (same statistic as the theoretical max)."""
    cycle_mean = _compute_cycle_mean()
    return 1000.0 / cycle_mean if cycle_mean > 0 else 0.0


def _compute_theoretical_max_bps() -> float:
    """Ceiling BPS = 1 / max(bet_delay, rolling api avg). 0 when both are 0."""
    floor = max(float(state.bet_delay or 0), _compute_api_rolling_avg() / 1000.0)
    return (1.0 / floor) if floor > 0 else 0.0


def _compute_efficiency_pct() -> float:
    """Recent BPS as % of the theoretical max (capped at 100); session BPS until the window fills."""
    tmax = _compute_theoretical_max_bps()
    if tmax <= 0:
        return 0.0
    recent = _compute_recent_bps() or state.bets_per_second
    return min(100.0, recent / tmax * 100)


def _record_api_ms(dt_ms: float) -> None:
    """Update every API-timing counter from one request."""
    with state.lock:
        state._last_api_ms   = dt_ms
        state._api_ms_total += dt_ms
        state._api_ms_count += 1
        state._api_samples.append(dt_ms)
        if dt_ms > state.peak_api_ms:
            state.peak_api_ms = dt_ms
        if state.bet_delay > 0 and dt_ms <= state.bet_delay * 1000:
            state._on_target_count += 1


def _record_cycle_ms(cycle_ms: float) -> None:
    """Push one full-iteration cycle time; overhead = cycle p50 - api p50 - delay."""
    with state.lock:
        state._cycle_samples.append(cycle_ms)
        state._cycle_ms = _compute_cycle_p50()
        api_ref = _compute_api_p50() if state._api_samples else state._last_api_ms
        state._cycle_overhead_ms = max(
            0.0, state._cycle_ms - api_ref - float(state.bet_delay or 0) * 1000)


def _metrics_state_dict() -> dict:
    """Metric keys written to the state file (read by --status / --diagnose)."""
    ot = _compute_on_target_pct()
    return {
        "api_ms": round(state._last_api_ms, 1),
        "api_avg_ms": round(_compute_api_avg(), 1),
        "api_peak_ms": round(state.peak_api_ms, 1),
        "api_p50_ms": round(_compute_api_p50(), 1),
        "api_p99_ms": round(_compute_api_p99(), 1),
        "on_target_pct": round(ot, 1) if ot is not None else None,
        "cycle_ms": round(state._cycle_ms, 1),
        "overhead_ms": round(state._cycle_overhead_ms, 1),
        "cycle_p50_ms": round(_compute_cycle_p50(), 1),
        "cycle_p99_ms": round(_compute_cycle_p99(), 1),
        "cycle_mean_ms": round(_compute_cycle_mean(), 1),
        "cycle_sample_count": len(state._cycle_samples),
        "bets_per_second_recent": round(_compute_recent_bps(), 2),
        "efficiency_pct": round(_compute_efficiency_pct(), 1),
        "theoretical_max_bps": round(_compute_theoretical_max_bps(), 2),
        "db_write_mode": state.db_write_mode,
        "db_writer_stats": dict(_bet_writer_stats),
        "db_writer_queue": _bet_write_queue.qsize(),
    }


# ===========================================================
#  RISKY CONFIG  (v1.9.0 — WolfBet v2.22.1 / v2.32.1)
# ===========================================================
# Strategy keys here: 2 Martingale, 3 Anti-Martingale, 6 Delay Martingale, 7 Rule-Based
# (WolfBet: 7 Delay Martingale, 8 rules).
def _check_risky_config() -> list:
    """Heads-up strings for configs known to drain balance fast. Never blocks."""
    warnings = []
    sk = state.strategy_key
    loss_mult = float(state.loss_mult or 0)
    mult = float(state.multiplier_target or 0)
    bal = float(state.current_balance or state.start_balance or 0)

    if sk == "2" and loss_mult >= 2.5:
        warnings.append(
            f"Aggressive Martingale (loss_mult={loss_mult}) — a 5-loss streak needs "
            f"~{loss_mult**5:.0f}× base_bet in reserve."
        )
    if sk in ("2", "6") and state.max_loss is None:
        warnings.append(
            "No `max_loss` set — Martingale-style strategies can drain balance "
            "on a bad streak. Consider `t maxloss <amount>` (or set it in the wizard) "
            "to establish a floor."
        )
    # v1.10.0 — with a lane budget only the lane's share backs the chain.
    lane_budget = split_budget_on(state.lanes_count, state.lane_split)
    bank = bal / state.lanes_count if lane_budget else bal
    depth = martingale_chain_depth(bank, float(state.base_bet or 0), loss_mult)
    if sk in ("2", "6") and state.streak_reset_losses and state.max_loss is None and depth:
        warnings.append(
            f"Streak reset with no `max_loss`: {'lane share' if lane_budget else 'balance'} "
            f"covers a {depth}-loss chain. "
            f"Loss {depth + 1} in a row ends the session with insufficient balance."
        )
    if sk == "3" and state.max_profit is None:
        warnings.append(
            "Anti-Martingale grows bets on wins with no lock-in. "
            "Consider `t maxprofit <amount>` to bank a target."
        )
    if bal > 0 and state.base_bet > 0.01 * bal:
        pct = state.base_bet / bal * 100
        warnings.append(
            f"Base bet is {pct:.1f}% of current balance ({state.base_bet:.8f} of "
            f"{bal:.8f}) — small bankrolls drain in ~7 losses at this size."
        )
    if mult > 10:
        warnings.append(
            f"Multiplier {mult}x → win chance ≈ {99.0 / mult:.1f}%. "
            "Most sessions at this multiplier end in the red."
        )
    if state.recurring and state.max_profit is None and state.max_bets is None:
        rd = float(state.recurring_delay or 0)
        gap = f"delay {rd:.0f}s between cycles" if rd > 0 else "back-to-back (no delay)"
        warnings.append(
            "Recurring restart is on with no max_profit and no max_bets — "
            f"sessions could restart indefinitely ({gap}). "
            "Consider setting at least one cap."
        )
    if sk == "7" and state.max_bets is None:
        warnings.append(
            "Custom rules (Strategy 7) with no `max_bets` cap can loop. "
            "Consider setting `max_bets` for safety."
        )
    if state.basebet_pct >= 0.05:
        warnings.append(
            f"basebet_pct = {state.basebet_pct*100:.1f}% of balance — "
            "the base bet grows quickly on winning streaks."
        )
    if state.proxy and state.proxy.startswith("http://"):
        warnings.append(
            f"proxy scheme is http:// ({mask_proxy(state.proxy)}) — "
            "credentials + traffic travel unencrypted between you and the proxy. "
            "Prefer https:// or socks5:// when possible."
        )
    return warnings


# ===========================================================
#  RUNTIME TWEAK  (v1.9.0 — WolfBet v2.19.0; mirrors tg cmd_tweak)
# ===========================================================
_TWEAK_ALIAS = {
    "delay":       "bet_delay",
    "sdloss":      "streak_delay_loss",
    "sdwin":       "streak_delay_win",
    "sdbets":      "streak_delay_bets",
    "sbloss":      "streakbet_loss",
    "bbpct":       "basebet_pct",
    "maxprofit":   "max_profit",
    "maxloss":     "max_loss",
    "maxbets":     "max_bets",
    "maxwins":     "max_wins",
    "minbalance":  "stop_on_balance",
    "msbets":      "milestone_bets",
    "mswins":      "milestone_wins",
    "mslosses":    "milestone_losses",
    "msprofit":    "milestone_profit",
    "pi":          "profit_increment",
    "pt":          "profit_threshold",
    "basebet":     "base_bet",
    "multiplier":  "multiplier_target",
    "mult":        "multiplier_target",
    "lossmult":    "loss_mult",
    "winmult":     "win_mult",
    "recdelay":    "recurring_delay",
    "recurringdelay": "recurring_delay",
    "srloss":      "streak_reset_losses",
    "srdelay":     "streak_reset_delay",
    "purgedays":   "purge_days",
    "dbmode":      "db_write_mode",
    "dbwritemode": "db_write_mode",
}
_TWEAK_PARAMS = {
    "bet_delay", "basebet_pct",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "recurring", "recurring_delay",
    "streak_reset_losses", "streak_reset_delay",
    "base_bet", "multiplier_target", "loss_mult", "win_mult",
    "proxy", "purge_days", "db_write_mode",
}
_TWEAK_OFF_VALUES = (None, "", "off", "none", "0", "disable", "false", "no", "n")


def _dice_target_for(mult: float, condition: str) -> float:
    """Dice target for a payout multiplier (the wizard's 99 / mult rule)."""
    wc = 99.0 / mult
    return round(100.0 - wc, 2) if condition == "above" else round(wc, 2)


def _apply_mutation(changes: dict) -> list:
    """Apply {param: value} to state; return status lines. Raises ValueError on bad input."""
    msgs = []

    def _is_off(v):
        return v is None or (isinstance(v, str) and v.strip().lower() in _TWEAK_OFF_VALUES)

    def _clamped(name: str, raw: float, lo: float, hi: float) -> float:
        val, msg = clamp(name, raw, lo, hi)
        if msg:
            msgs.append(msg)
        return val

    def _num(name, v, cast=float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, got {v!r}")

    if "bet_delay" in changes:
        v = changes["bet_delay"]
        state.bet_delay = 0.0 if _is_off(v) else max(0.0, _num("bet_delay", v))
        msgs.append(f"Delay: {state.bet_delay:.3f}s")

    if "basebet_pct" in changes:
        v = changes["basebet_pct"]
        state.basebet_pct = 0.0 if _is_off(v) else _num("basebet_pct", v)
        if state.basebet_pct > 0 and state.current_balance > 0:
            _apply_basebet_pct()
            msgs.append(f"Basebet pct: {state.basebet_pct*100:.4f}% → base={state.base_bet:.8f}")
        else:
            msgs.append("Basebet pct: off")

    for attr, label in [("streak_delay_loss", "Streak delay loss"),
                        ("streak_delay_win",  "Streak delay win"),
                        ("streak_delay_bets", "Streak delay bets"),
                        ("streakbet_loss",    "Streakbet loss")]:
        if attr in changes:
            v = changes[attr]
            setattr(state, attr, parse_streak_delay(None if _is_off(v) else v))
            tup = getattr(state, attr)
            msgs.append(f"{label}: every {tup[0]} → {tup[1]:.3f}" if tup[0] > 0 else f"{label}: off")

    for attr, label, cast in [("max_profit",      "Max profit",  float),
                              ("max_loss",        "Max loss",    float),
                              ("max_bets",        "Max bets",    int),
                              ("max_wins",        "Max wins",    int),
                              ("stop_on_balance", "Min balance", float)]:
        if attr in changes:
            v = changes[attr]
            if _is_off(v):
                setattr(state, attr, None)
                msgs.append(f"{label}: off")
            else:
                setattr(state, attr, _num(attr, v, cast))
                msgs.append(f"{label}: {getattr(state, attr)}")

    for attr, label in [("milestone_bets",   "Milestone bets"),
                        ("milestone_wins",   "Milestone wins"),
                        ("milestone_losses", "Milestone losses")]:
        if attr in changes:
            v = changes[attr]
            setattr(state, attr, 0 if _is_off(v) else _num(attr, v, int))
            msgs.append(f"{label}: {getattr(state, attr) or 'off'}")
    if "milestone_profit" in changes:
        v = changes["milestone_profit"]
        state.milestone_profit = 0.0 if _is_off(v) else _num("milestone_profit", v)
        msgs.append(f"Milestone profit: {state.milestone_profit:.8f}"
                    if state.milestone_profit else "Milestone profit: off")

    if "profit_increment" in changes:
        v = changes["profit_increment"]
        state.profit_increment = None if _is_off(v) else _num("profit_increment", v)
        msgs.append(f"Profit increment: {state.profit_increment:.8f}"
                    if state.profit_increment else "Profit increment: off")
    if "profit_threshold" in changes:
        v = changes["profit_threshold"]
        state.profit_threshold = None if _is_off(v) else _num("profit_threshold", v)
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        msgs.append(f"Profit threshold: {state.profit_threshold:.8f}"
                    if state.profit_threshold else "Profit threshold: off")

    if "recurring" in changes:
        state.recurring = str(changes["recurring"]).strip().lower() in ("on", "true", "yes", "y", "1")
        msgs.append(f"Recurring: {'on' if state.recurring else 'off'}")

    for attr, label in [("recurring_delay", "Recurring delay"),
                        ("streak_reset_delay", "Streak reset delay")]:
        if attr in changes:
            v = changes[attr]
            if _is_off(v):
                setattr(state, attr, 0.0)
            else:
                try:
                    raw = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{attr} must be a number 0-{RECURRING_DELAY_MAX}, got {v!r}")
                setattr(state, attr, _clamped(attr, raw, 0, RECURRING_DELAY_MAX))
            val = getattr(state, attr)
            msgs.append(f"{label}: {val:.0f}s" if val > 0 else f"{label}: off")

    if "streak_reset_losses" in changes:
        v = changes["streak_reset_losses"]
        if _is_off(v):
            state.streak_reset_losses = 0
        else:
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"streak_reset_losses must be an integer >= 1, got {v!r}")
            if n < 1:
                raise ValueError(f"streak_reset_losses must be an integer >= 1, got {v!r}")
            state.streak_reset_losses = n
        msgs.append(f"Streak reset: after {state.streak_reset_losses} losses"
                    if state.streak_reset_losses else "Streak reset: off")

    if "proxy" in changes:
        try:
            state.proxy = validate_proxy(changes["proxy"])
        except ValueError as e:
            raise ValueError(f"proxy: {e}")
        _apply_proxy_to_session()
        msgs.append(f"Proxy: {mask_proxy(state.proxy)}")

    if "purge_days" in changes:
        v = changes["purge_days"]
        if isinstance(v, str) and v.strip().lower() in ("off", "none", "false", "disable", "no"):
            state.purge_days = 3
        else:
            try:
                raw = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"purge_days must be an integer 1-30, got {v!r}")
            state.purge_days = int(_clamped("purge_days", raw, 1, 30))
        msgs.append(f"Purge days: {state.purge_days}")

    if "db_write_mode" in changes:
        v = str(changes["db_write_mode"]).strip().lower()
        if v not in ("async", "sync"):
            raise ValueError(f"db_write_mode must be 'async' or 'sync', got {v!r}")
        state.db_write_mode = v
        msgs.append(f"DB write mode: {state.db_write_mode}")

    # Strategy config — applies to the next bet (no server-side auto-bet to restart).
    if "base_bet" in changes:
        bet = _num("base_bet", changes["base_bet"])
        if bet < 0:
            raise ValueError(f"base_bet must be >= 0, got {bet!r}")
        if 0 < bet < MIN_BET:
            msgs.append(f"⚠ base_bet raised to the minimum {MIN_BET}")
            bet = MIN_BET
        with state.lock:
            state.base_bet = bet
            state.current_bet = bet
        msgs.append(f"Base bet: {state.base_bet:.8f}")
    if "multiplier_target" in changes:
        m = _num("multiplier", changes["multiplier_target"])
        if m < 1.01:
            raise ValueError(f"multiplier must be at least 1.01, got {m!r}")
        state.multiplier_target = m
        msgs.append(f"Multiplier: {state.multiplier_target:.2f}x")
        if state.game == "dice":
            state.dice_target = _dice_target_for(m, state.dice_condition)
            msgs.append(f"Dice target: {state.dice_condition} {state.dice_target}")
    if "loss_mult" in changes:
        state.loss_mult = _num("loss_mult", changes["loss_mult"])
        msgs.append(f"Loss mult: {state.loss_mult:.2f}")
    if "win_mult" in changes:
        state.win_mult = _num("win_mult", changes["win_mult"])
        msgs.append(f"Win mult: {state.win_mult:.2f}")

    return msgs

# ===========================================================
#  API HELPERS  (GraphQL)
# ===========================================================
def _headers() -> dict:
    import uuid
    return {
        "Content-Type":    "application/json",
        "Accept":          "application/graphql-response+json,application/json;q=0.9",
        "Authorization":   f"Bearer {state.bearer_token}",
        "Origin":          "https://shuffle.com",
        "Referer":         f"https://shuffle.com/games/originals/{state.game}",
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "x-correlation-id": str(uuid.uuid4()),
    }

def _gql_payload(query: str, variables: dict, operation_name: str) -> dict:
    return {
        "operationName": operation_name,
        "query": query,
        "variables": variables,
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.4"}},
    }

def _gql_post(query: str, variables: dict, operation_name: str) -> Optional[dict]:
    """Execute a GraphQL mutation/query. Retries once on connection reset."""
    global _http
    payload = _gql_payload(query, variables, operation_name)
    for attempt in range(2):
        try:
            r = _http.post(API_URL, headers=_headers(), json=payload, timeout=15)
            break
        except (ConnectionError, ConnectionResetError, requests.exceptions.ConnectionError) as e:
            if attempt == 0:
                logger.warning("Connection reset — recreating HTTP session and retrying")
                try:
                    _http.close()
                except Exception:
                    pass
                _http = _create_http()
                continue
            raise ConnectionError(f"Connection failed: {e}")
    if r.status_code != 200:
        body = r.text[:300] if r.text else "(empty)"
        raise ConnectionError(f"HTTP {r.status_code}: {body}")
    data = r.json()
    if "errors" in data:
        raise ConnectionError(f"GraphQL errors: {json.dumps(data['errors'])[:300]}")
    return data.get("data", {})

_PROFILE_QUERY = """query GetMyProfile {
  me {
    id
    username
    account {
      id
      balances {
        currency
        amount: availableAmount
        __typename
      }
      __typename
    }
    __typename
  }
}"""


def api_get_balances() -> list:
    """Fetch all balances via GetMyProfile query."""
    try:
        data = _gql_post(_PROFILE_QUERY, {}, "GetMyProfile")
        me = data.get("me", {})
        account = me.get("account", {})
        balances = account.get("balances", [])
        return [{"currency": b.get("currency", ""),
                 "amount": float(b.get("amount", 0))}
                for b in balances if float(b.get("amount", 0)) > 0]
    except Exception as e:
        logger.debug("Balance fetch failed: %s", e)
    return []

def api_test_connection() -> bool:
    """Test auth by fetching the user profile (read-only, no bet placed)."""
    try:
        balances = api_get_balances()
        if isinstance(balances, list):
            return True
        raise Exception("Unexpected response from profile query")
    except Exception as e:
        raise Exception(f"Connection failed: {e}")

def api_place_bet(amount: float) -> Optional[dict]:
    """Place a bet via GraphQL. Returns parsed result dict or None."""
    if 0 < amount < MIN_BET:
        amount = MIN_BET

    game_info = GAMES[state.game]
    response_key = game_info["response_key"]
    build = game_info["build_query"]
    parse = game_info["parse_result"]

    saved = state.current_bet
    state.current_bet = amount
    query, variables = build(state)
    state.current_bet = saved

    logger.debug("BET [%s] variables=%s", state.game, json.dumps(variables))
    try:
        t0 = time.time()
        data = _gql_post(query, variables, game_info["operation_name"])
        _record_api_ms((time.time() - t0) * 1000)
        if data is None:
            with state.lock:
                state.last_error = "Empty response"
            return None
        raw = data.get(response_key, data)
        if not raw:
            logger.error("Empty response key %s: %s", response_key, data)
            with state.lock:
                state.last_error = f"Empty {response_key}"
            return None
        result = parse(data)
        logger.debug("BET OK  result=%s win=%s", result["result_display"], result["is_win"])
        return result
    except ConnectionError as e:
        err = str(e)
        if "429" in err:
            logger.warning("RATE LIMIT 429")
            with state.lock:
                state.last_error = "Rate limit 429 -- backing off..."
        elif "insufficient" in err.lower():
            logger.warning("INSUFFICIENT BALANCE")
            with state.lock:
                state.last_error = "Insufficient balance"
                state._insufficient_balance = True
        else:
            logger.error("API error: %s", err)
            with state.lock:
                state.last_error = err[:120]
    except requests.exceptions.Timeout:
        logger.warning("REQUEST TIMEOUT on bet #%d", state.total_bets + 1)
        with state.lock:
            state.last_error = "Request timeout -- retrying..."
    except Exception as e:
        logger.exception("UNEXPECTED ERROR placing bet: %s", e)
        with state.lock:
            state.last_error = str(e)[:120]
    return None

# ===========================================================
#  STRATEGY ENGINE
# ===========================================================
def compute_next_bet(last_result: str) -> float:
    key  = state.strategy_key
    base = state.base_bet
    cur  = state.current_bet
    lm   = state.loss_mult
    wm   = state.win_mult

    if key == "1":   # Flat
        return base
    elif key == "2": # Martingale
        return (cur * lm) if last_result == "loss" else base
    elif key == "3": # Anti-Martingale
        return (cur * wm) if last_result == "win" else base
    elif key == "4": # D'Alembert
        if last_result == "loss":
            state.dalembert_unit += 1
        elif last_result == "win" and state.dalembert_unit > 0:
            state.dalembert_unit -= 1
        return base * (1 + state.dalembert_unit)
    elif key == "5": # Paroli
        if last_result == "win":
            state.paroli_count += 1
            if state.paroli_count >= 3:
                state.paroli_count = 0
                return base
            return cur * wm
        else:
            state.paroli_count = 0
            return base
    elif key == "6": # Delay Martingale
        if last_result == "win":
            return base
        consec_losses = abs(min(state.current_streak, 0))
        if consec_losses <= state.delay_martin_threshold:
            return base
        return cur * lm
    elif key == "7": # Rule-Based
        return cur
    return base

def should_stop() -> tuple:
    p = state.profit
    b = state.current_balance
    if state.max_profit is not None and p >= state.max_profit:
        return True, f"Profit target reached: {p:+.8f}"
    if state.max_loss is not None and p <= -abs(state.max_loss):
        return True, f"Max loss hit: {p:+.8f}"
    if state.max_bets is not None and state.total_bets >= state.max_bets:
        return True, f"Max bets reached: {state.total_bets}"
    if state.max_wins is not None and state.wins >= state.max_wins:
        return True, f"Max wins reached: {state.wins}"
    if state.stop_on_balance is not None and b <= state.stop_on_balance:
        return True, f"Balance floor hit: {b:.8f}"
    # Streak reset: only on the win that broke an N+ loss streak, profit >= 0.
    if streak_reset_triggered(state.current_streak, state.last_broken_loss_streak,
                              state.streak_reset_losses, p):
        return True, streak_reset_stop_reason(state.last_broken_loss_streak)
    # v1.10.0 — lane budget (WolfBet v2.35.0). Not on the restart whitelist.
    over, reason = lane_budget_exceeded(state.current_bet, _lane_equity())
    if over:
        return True, reason
    return False, ""


def _apply_basebet_pct() -> bool:
    """Recompute base_bet as basebet_pct × balance. Returns True if it changed."""
    if state.basebet_pct <= 0 or state.current_balance <= 0:
        return False
    # v1.10.0 — the lane bet split divides it too (WolfBet v2.36.0).
    # v1.11.0 — a split bet that rounds to 0 or is below MIN_BET stays full.
    new_base, note = split_base_bet(
        round(state.current_balance * state.basebet_pct, 8),
        state.lanes_count, state.lane_split, False, MIN_BET)
    if note:
        logger.warning(note)
    if new_base <= 0 or abs(new_base - state.base_bet) < 1e-12:
        return False
    with state.lock:
        state.base_bet    = new_base
        state.current_bet = new_base
    logger.info("BASEBET_PCT: base_bet -> %.8f (balance %.8f x %.6f)",
                new_base, state.current_balance, state.basebet_pct)
    return True


def _check_milestone(bet_state: str) -> Optional[str]:
    """Milestone text when a bets / wins / losses / profit threshold is crossed."""
    if state.milestone_bets > 0 and state.total_bets > 0 and state.total_bets % state.milestone_bets == 0:
        return f"{state.total_bets} bets"
    if (state.milestone_wins > 0 and bet_state == "win"
            and state.wins > 0 and state.wins % state.milestone_wins == 0):
        return f"{state.wins} wins"
    if (state.milestone_losses > 0 and bet_state != "win"
            and state.losses > 0 and state.losses % state.milestone_losses == 0):
        return f"{state.losses} losses"
    if state.milestone_profit > 0 and state.profit > 0:
        crossed = int(state.profit / state.milestone_profit) * state.milestone_profit
        if crossed > state._last_profit_milestone:
            state._last_profit_milestone = crossed
            return f"{crossed:.8f} profit"
    return None


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in short steps; return early once the bot is told to stop."""
    remaining = float(seconds)
    while remaining > 0 and state.running:
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step


def _reset_session_for_recurring(delay: float) -> None:
    """Pause, then open a new sessions row with fresh counters (config kept).

    The bet-end time is frozen before the pause so uptime and ended_at cover
    betting only. A stop during the pause opens no new row.
    """
    bet_end = time.time()
    with state.lock:
        state.session_bet_ended_at     = bet_end
        state.session_pause_started_at = bet_end
        state.session_pause_total      = delay
    if delay > 0:
        state.status = f"Recurring pause {delay:.0f}s before next cycle..."
        logger.info("RECURRING pause %.0fs before next cycle", delay)
        _interruptible_sleep(delay)
    if not state.running:
        return
    with state.lock:
        state.session_bet_ended_at     = None
        state.session_pause_started_at = None
        state.session_id     = _db_start_session()
        state.session_start  = time.time()
        state.total_bets     = 0
        state.wins           = 0
        state.losses         = 0
        state.lane_run_profit += state.profit   # lane equity spans its cycles
        state.profit         = 0.0
        state.wagered        = 0.0
        state.current_streak = 0
        state.max_win_streak = 0
        state.max_loss_streak = 0
        state.last_broken_loss_streak = 0
        state.highest_bet    = 0.0
        state.highest_win    = 0.0
        state.biggest_loss   = 0.0
        state.recent_bets.clear()
        state.profit_history.clear()
        state.profit_history.append(0.0)
        state.start_balance   = state.current_balance
        state.highest_balance = state.current_balance
        state.lowest_balance  = state.current_balance
        # bumped base_bet is kept across cycles; the milestone is re-armed
        state.current_bet    = state.base_bet
        state._last_profit_milestone = 0.0
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        state.dalembert_unit = 0
        state.paroli_count   = 0
        state.bets_per_second = state.bets_per_minute = 0.0
        state.peak_bps = state.peak_bpm = 0.0
        state.low_bps = state.low_bpm = float("inf")
        state._bets_this_sec = state._bets_this_min = 0
        state._current_sec = state._current_min = 0
        state.consecutive_errors = 0
        state.backoff_delay  = 1.0
        state.last_error     = ""
        state.status = f"Recurring restart -- session #{state.session_id}"
    logger.info("RECURRING RESTART complete: new session #%d, base_bet=%.8f, balance=%.8f",
                state.session_id, state.base_bet, state.current_balance)

# ===========================================================
#  BETTING LOOP  (runs in background thread)
# ===========================================================
def betting_loop():
    last_result = "none"

    while state.running:
        if state.paused:
            state.status = "PAUSED -- press [R] to resume"
            time.sleep(0.4)
            continue

        cycle_t0 = time.monotonic()
        stop, reason = should_stop()
        if stop:
            if should_restart(reason, state.profit, state.recurring, state.streak_reset_losses):
                logger.info("RECURRING RESTART after: %s", reason)
                _db_save_session()
                _reset_session_for_recurring(
                    restart_delay_for(reason, state.recurring_delay, state.streak_reset_delay))
                last_result = "none"
                continue
            state.running = False
            state.status  = f"STOPPED: {reason}"
            _db_save_session()
            break

        sd = streak_delay_for(state.current_streak, state.total_bets,
                              state.streak_delay_loss, state.streak_delay_win,
                              state.streak_delay_bets)
        if sd > 0:
            state.status = f"Streak delay {sd:.2f}s (streak {state.current_streak})"
            _interruptible_sleep(sd)
            if not state.running:
                break

        if state.basebet_pct > 0 and state.total_bets > 0 and state.total_bets % 10 == 0:
            _apply_basebet_pct()

        if state.bet_delay > 0:
            time.sleep(state.bet_delay)

        state.status = f"Placing {state.game} bet #{state.total_bets + 1}..."
        result = api_place_bet(state.current_bet)

        if result is None:
            if state._insufficient_balance:
                state.running = False
                state.status = "STOPPED: Insufficient balance"
                _db_save_session()
                break
            with state.lock:
                state.consecutive_errors += 1
                state.backoff_delay = min(
                    state.backoff_delay * 2,
                    state.max_backoff,
                )
            time.sleep(state.backoff_delay)
            continue

        amount_used    = result["amount"]
        payout         = result["payout"]
        is_win         = result["is_win"]
        result_display = result["result_display"]
        result_value   = result["result_value"]

        # Use afterBalance from API if available
        after_balance = result.get("after_balance")

        bet_state   = "win" if is_win else "loss"
        raw_profit  = payout - amount_used

        with state.lock:
            state.consecutive_errors = 0
            state.backoff_delay      = 1.0

            state.total_bets     += 1
            state.wagered        += amount_used
            state.profit         += raw_profit

            if after_balance is not None and after_balance > 0:
                state.current_balance = after_balance
            else:
                state.current_balance += raw_profit

            new_balance = state.current_balance

            state.current_streak, broken = record_outcome(state.current_streak, bet_state == "win")
            if broken is not None:
                state.last_broken_loss_streak = broken
            if bet_state == "win":
                state.wins += 1
                if raw_profit > state.highest_win:
                    state.highest_win = raw_profit
                state.max_win_streak = max(state.max_win_streak, state.current_streak)
            else:
                state.losses += 1
                if amount_used > state.biggest_loss:
                    state.biggest_loss = amount_used
                state.max_loss_streak = max(state.max_loss_streak, abs(state.current_streak))

            if amount_used > state.highest_bet:
                state.highest_bet = amount_used
            if new_balance > state.highest_balance:
                state.highest_balance = new_balance
            if new_balance < state.lowest_balance:
                state.lowest_balance = new_balance

            # profit-based base bet increment
            if (state.profit_increment and state.profit_threshold
                    and state.profit >= state.next_profit_milestone):
                state.base_bet += state.profit_increment
                state.next_profit_milestone += state.profit_threshold
                logger.info("PROFIT INCREMENT: base_bet -> %.8f", state.base_bet)

            if state.total_bets % 5 == 0:
                state.profit_history.append(state.profit)

            game_label = GAMES[state.game]["label"]
            extra_info = ""
            if state.game == "dice":
                extra_info = f" | {state.dice_condition} {state.dice_target}"

            state.recent_bets.append({
                "n":     state.total_bets,
                "time":  datetime.now().isoformat(),
                "amt":   amount_used,
                "roll":  result_display,
                "state": bet_state,
                "pnl":   raw_profit,
                "bal":   new_balance,
            })

            # bets-per-second / bets-per-minute
            now = time.time()
            elapsed = now - state.session_start
            if elapsed > 0:
                state.bets_per_second = state.total_bets / elapsed
                state.bets_per_minute = state.bets_per_second * 60

            sec_key = int(now)
            min_key = int(now) // 60
            if sec_key != state._current_sec:
                if state._current_sec > 0 and state._bets_this_sec > 0:
                    if state._bets_this_sec > state.peak_bps:
                        state.peak_bps = state._bets_this_sec
                    if state._bets_this_sec < state.low_bps:
                        state.low_bps = state._bets_this_sec
                state._current_sec = sec_key
                state._bets_this_sec = 1
            else:
                state._bets_this_sec += 1

            if min_key != state._current_min:
                if state._current_min > 0 and state._bets_this_min > 0:
                    if state._bets_this_min > state.peak_bpm:
                        state.peak_bpm = state._bets_this_min
                    if state._bets_this_min < state.low_bpm:
                        state.low_bpm = state._bets_this_min
                state._current_min = min_key
                state._bets_this_min = 1
            else:
                state._bets_this_min += 1

            sign  = "+" if raw_profit >= 0 else ""
            emoji = "W" if bet_state == "win" else "L"
            state.status = (
                f"{emoji} {'WIN' if bet_state=='win' else 'LOSS'} | "
                f"Roll: {result_display}{extra_info} | "
                f"Bet: {amount_used:.8f} | "
                f"P/L: {sign}{raw_profit:.8f} | "
                f"Bal: {new_balance:.8f}"
            )
            state.last_error = ""

        _db_save_bet_dispatch(result, raw_profit, new_balance)

        # Rule-Based: apply custom rules after bet
        if state.strategy_key == "7" and state.custom_rules:
            apply_rules(bet_state)

        ms_reason = _check_milestone(bet_state)
        if ms_reason:
            logger.info("MILESTONE: %s (balance %.8f, profit %+.8f)",
                        ms_reason, state.current_balance, state.profit)
            with state.lock:
                state.status = f"Milestone: {ms_reason}"

        # calculate next bet
        raw_next  = compute_next_bet(bet_state)
        next_bet  = max(state.base_bet, raw_next)
        sb_factor = streakbet_loss_factor(state.current_streak, state.streakbet_loss)
        if sb_factor != 1.0:
            next_bet = max(state.base_bet, next_bet * sb_factor)
            logger.info("STREAKBET_LOSS: current_bet x%.2f -> %.8f (loss streak %d)",
                        sb_factor, next_bet, state.current_streak)
        if 0 < next_bet < MIN_BET:
            next_bet = MIN_BET
        state.current_bet = next_bet
        last_result = bet_state
        _record_cycle_ms((time.monotonic() - cycle_t0) * 1000)

# ===========================================================
#  DATABASE I/O
# ===========================================================
def _db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    # v1.5.0 — mirrors wolfbet v2.27.0. WAL-safe.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -32000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn

def _db_start_session() -> int:
    conn = _db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions
            (started_at, currency, game, strategy, base_bet, multiplier, start_balance, lane)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        state.currency, state.game, state.strategy,
        state.base_bet, state.multiplier_target, state.start_balance, state.lane,
    ))
    conn.commit()
    sid = c.lastrowid
    conn.close()
    return sid

_BET_INSERT_SQL = (
    "INSERT INTO bets "
    "(session_id, timestamp, game, amount, multiplier_target, "
    " result_value, result_display, state, profit, balance_after) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def _make_bet_row(result: dict, profit: float, balance: float) -> tuple:
    """The 10-tuple for both the sync and the async INSERT paths."""
    return (
        state.session_id,
        datetime.now().isoformat(),
        state.game,
        result["amount"],
        state.multiplier_target,
        result["result_value"],
        result["result_display"],
        "win" if result["is_win"] else "loss",
        profit,
        balance,
    )


def _db_save_bet(result: dict, profit: float, balance: float):
    _db_save_bet_row(_make_bet_row(result, profit, balance))


def _db_save_bet_row(row: tuple):
    try:
        conn = _db_conn()
        conn.execute(_BET_INSERT_SQL, row)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_bet failed: %s", e)


# -- v1.9.0 async bet writer (WolfBet v2.27.0) --
# Bounded queue: if the writer stalls, rows fall back to the sync path, never dropped.
_bet_write_queue: queue.Queue = queue.Queue(maxsize=1000)
_bet_writer_thread: Optional[threading.Thread] = None
_bet_writer_stop = threading.Event()
_bet_writer_stats = {
    "queued": 0,
    "flushed": 0,
    "batches": 0,
    "fallback_sync": 0,
    "queue_high_watermark": 0,
}
_BET_WRITER_BATCH_SIZE   = 32
_BET_WRITER_BATCH_WINDOW = 0.1   # seconds


def _bet_writer_worker():
    """Drain _bet_write_queue in batches; one commit per batch."""
    try:
        conn = _db_conn()
    except Exception as e:
        logger.error("bet writer failed to open DB (%s) — rows fall back to sync on overflow", e)
        return
    while not (_bet_writer_stop.is_set() and _bet_write_queue.empty()):
        batch = []
        deadline = time.monotonic() + _BET_WRITER_BATCH_WINDOW
        while len(batch) < _BET_WRITER_BATCH_SIZE:
            timeout = max(0.0, deadline - time.monotonic())
            if timeout == 0:
                break
            try:
                batch.append(_bet_write_queue.get(timeout=timeout))
            except queue.Empty:
                break
        if not batch:
            continue
        try:
            conn.executemany(_BET_INSERT_SQL, batch)
            conn.commit()
            _bet_writer_stats["batches"] += 1
            _bet_writer_stats["flushed"] += len(batch)
        except Exception as e:
            logger.warning("bet writer batch failed (%d rows dropped): %s", len(batch), e)
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn = _db_conn()
            except Exception as reopen_err:
                logger.error("bet writer reopen failed: %s — exiting worker", reopen_err)
                return
    try:
        conn.close()
    except Exception:
        pass


def _start_bet_writer():
    """Spawn the writer thread if not already running. Idempotent."""
    global _bet_writer_thread
    if _bet_writer_thread is not None and _bet_writer_thread.is_alive():
        return
    _bet_writer_stop.clear()
    _bet_writer_thread = threading.Thread(target=_bet_writer_worker, daemon=True, name="BetWriter")
    _bet_writer_thread.start()


def _stop_bet_writer(timeout: float = 5.0):
    """Tell the writer to drain the queue, then join (bounded wait)."""
    global _bet_writer_thread
    _bet_writer_stop.set()
    if _bet_writer_thread is not None and _bet_writer_thread.is_alive():
        _bet_writer_thread.join(timeout=timeout)
        if _bet_writer_thread.is_alive():
            logger.warning("bet writer did not drain within %.1fs; %d rows may be lost",
                           timeout, _bet_write_queue.qsize())
    _bet_writer_thread = None


def _db_save_bet_async(result: dict, profit: float, balance: float):
    """Enqueue a bet row; on a full queue write it synchronously (no row lost)."""
    row = _make_bet_row(result, profit, balance)
    try:
        _bet_write_queue.put_nowait(row)
        _bet_writer_stats["queued"] += 1
        qs = _bet_write_queue.qsize()
        if qs > _bet_writer_stats["queue_high_watermark"]:
            _bet_writer_stats["queue_high_watermark"] = qs
    except queue.Full:
        _bet_writer_stats["fallback_sync"] += 1
        _db_save_bet_row(row)


def _db_save_bet_dispatch(result: dict, profit: float, balance: float):
    """Async writer when db_write_mode is async and the writer runs; else sync."""
    if state.db_write_mode == "async" and _bet_writer_thread is not None:
        _db_save_bet_async(result, profit, balance)
    else:
        _db_save_bet(result, profit, balance)

def _db_save_session():
    try:
        conn = _db_conn()
        lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
        conn.execute("""
            UPDATE sessions SET
                ended_at=?, total_bets=?, wins=?, losses=?,
                profit=?, wagered=?, end_balance=?,
                max_win_streak=?, max_loss_streak=?,
                highest_balance=?, lowest_balance=?,
                highest_win=?, biggest_loss=?,
                bets_per_minute=?, bets_per_second=?,
                peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
            WHERE id=?
        """, (
            # frozen bet-end during a cycle pause, so ended_at excludes the pause
            (datetime.fromtimestamp(state.session_bet_ended_at)
             if state.session_bet_ended_at else datetime.now()).isoformat(),
            state.total_bets, state.wins, state.losses,
            state.profit, state.wagered, state.current_balance,
            state.max_win_streak, state.max_loss_streak,
            state.highest_balance, lo_bal,
            state.highest_win, state.biggest_loss,
            min(state.bets_per_second * 60, 999) if state.bets_per_second else state.bets_per_minute,
            state.bets_per_second,
            state.peak_bps,
            state.low_bps if state.low_bps != float("inf") else 0,
            state.peak_bpm,
            state.low_bpm if state.low_bpm != float("inf") else 0,
            state.session_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_session failed: %s", e)

def _db_get_history(limit: int = 12):
    conn = _db_conn()
    rows = conn.execute("""
        SELECT id, started_at, ended_at, currency, game, strategy,
               total_bets, profit, wagered, max_win_streak, max_loss_streak
        FROM sessions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows

# ===========================================================
#  STATE FILE
# ===========================================================
def _uptime_sec() -> int:
    return int((state.session_bet_ended_at or time.time()) - state.session_start)


def _pause_remaining_sec() -> int:
    if not state.session_pause_started_at:
        return 0
    left = state.session_pause_total - (time.time() - state.session_pause_started_at)
    return max(0, int(left))


def _save_state_file():
    try:
        data = {
            "pid": os.getpid(),
            "session_id": state.session_id,
            "running": state.running,
            "paused": state.paused,
            "currency": state.currency,
            "game": state.game,
            "strategy": state.strategy,
            "multiplier_target": state.multiplier_target,
            "dice_target": state.dice_target,
            "dice_condition": state.dice_condition,
            "base_bet": state.base_bet,
            "current_bet": state.current_bet,
            "total_bets": state.total_bets,
            "wins": state.wins,
            "losses": state.losses,
            "profit": state.profit,
            "wagered": state.wagered,
            "current_balance": state.current_balance,
            "start_balance": state.start_balance,
            "current_streak": state.current_streak,
            "max_win_streak": state.max_win_streak,
            "max_loss_streak": state.max_loss_streak,
            "highest_bet": state.highest_bet,
            "highest_win": state.highest_win,
            "biggest_loss": state.biggest_loss,
            "bets_per_minute": state.bets_per_minute,
            "bets_per_second": state.bets_per_second,
            "peak_bps": state.peak_bps,
            "low_bps": state.low_bps if state.low_bps != float("inf") else 0,
            "peak_bpm": state.peak_bpm,
            "low_bpm": state.low_bpm if state.low_bpm != float("inf") else 0,
            "highest_balance": state.highest_balance,
            "lowest_balance": state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance,
            "bet_delay": state.bet_delay,
            "loss_mult": state.loss_mult,
            "win_mult": state.win_mult,
            "strategy_key": state.strategy_key,
            "max_profit": state.max_profit,
            "max_loss": state.max_loss,
            "max_bets": state.max_bets,
            "max_wins": state.max_wins,
            "stop_on_balance": state.stop_on_balance,
            "profit_increment": state.profit_increment,
            "profit_threshold": state.profit_threshold,
            "rule_count": len(state.custom_rules) if state.strategy_key == "7" else 0,
            "rule_descriptions": [r.description for r in state.custom_rules] if state.strategy_key == "7" else [],
            # v1.7.0 — uptime freezes at bet-end during a cycle pause (WolfBet v2.28.4)
            "uptime_sec": _uptime_sec(),
            "run_uptime_sec": int(time.time() - state.run_started_at),
            "pause_remaining_sec": _pause_remaining_sec(),
            "pause_total_sec": int(state.session_pause_total),
            "recurring": state.recurring,
            "recurring_delay_sec": int(state.recurring_delay),
            "streak_reset_losses": state.streak_reset_losses,
            "streak_reset_delay_sec": int(state.streak_reset_delay),
            "next_profit_milestone": state.next_profit_milestone,
            # v1.9.0 — observability (WolfBet v2.17.0 / v2.30.3-v2.30.5 / v2.27.0)
            **_metrics_state_dict(),
            "purge_days": state.purge_days,
            # v1.10.0 lanes (WolfBet v2.35.0 / v2.36.0)
            "lane": state.lane,
            "lanes_count": state.lanes_count,
            "lane_split": state.lane_split,
            "lane_share": state.lane_share,
            "lane_equity": _lane_equity(),
            "proxy": mask_proxy(state.proxy) if state.proxy else "direct",
            "status": state.status,
            "last_error": state.last_error,
            "updated_at": datetime.now().isoformat(),
        }
        fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _cleanup_state_file():
    for p in (STATE_PATH, PID_PATH):
        try:
            os.remove(p)
        except OSError:
            pass

# ===========================================================
#  TUI -- BORDERLESS ULTRA-COMPACT DASHBOARD
# ===========================================================
SPARKLINE = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

def _sparkline(values) -> str:
    if not values or len(values) < 2:
        return "--"
    mn, mx = min(values), max(values)
    span   = mx - mn or 1
    return "".join(SPARKLINE[int((v - mn) / span * 7)] for v in values)

_RST  = "\033[0m"
_BOLD = "\033[1m"
_DIM  = "\033[2m"

_COLORS = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "white": "\033[37m",
    "bg_blue": "\033[44m", "bg_magenta": "\033[45m",
    "bold_red": "\033[1;31m", "bold_green": "\033[1;32m",
    "bold_yellow": "\033[1;33m", "bold_cyan": "\033[1;36m",
    "bold_white": "\033[1;37m",
    "dim_blue": "\033[2;34m", "dim_magenta": "\033[2;35m", "dim": "\033[2m",
}
_CLR_EOL = "\033[K"

def _a(color: str, text: str) -> str:
    code = _COLORS.get(color, "")
    return f"{code}{text}{_RST}" if code else text

def _fmt_speed() -> str:
    pbps = int(state.peak_bps) if state.peak_bps > 0 else 0
    lbps = int(state.low_bps) if state.low_bps != float("inf") else 0
    pbpm = int(state.peak_bpm) if state.peak_bpm > 0 else 0
    lbpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0
    return f"{lbps}-{pbps}/s {lbpm}-{pbpm}/m"

def _game_info_str() -> str:
    wc = _get_win_chance()
    if state.game == "dice":
        return f"Dice {state.dice_condition} {state.dice_target}  {state.multiplier_target}x  {wc:.1f}%"
    else:
        return f"Limbo {state.multiplier_target}x target  {wc:.1f}% chance"

def _pct_color(pct: float) -> str:
    if pct >= 95:
        return "bold_red"
    if pct >= 80:
        return "yellow"
    return "green"


def _build_config_progress_row(d: dict) -> str:
    """Live config-progress summary row (v1.5.0)."""
    segments = []

    profit_threshold = d.get("profit_threshold")
    profit_increment = d.get("profit_increment")
    if profit_threshold and profit_increment is not None:
        # v1.7.0 — progress inside the current bump window (WolfBet v2.30.1 / v2.30.7):
        # the bump code's next_profit_milestone, one threshold unit wide.
        threshold = float(profit_threshold)
        next_ms = float(d.get("next_profit_milestone") or threshold)
        profit  = float(d.get("profit", 0.0) or 0.0)
        window_start = max(0.0, next_ms - threshold)
        window_progress = max(0.0, profit - window_start)
        pct = min(100.0, window_progress / threshold * 100)
        segments.append(
            f"{_a('dim','Ms')} "
            f"{_a(_pct_color(pct), f'+{window_progress:.8f}/{threshold:.8f} ({pct:.0f}%)')}"
        )

    max_profit = d.get("max_profit")
    if max_profit is not None:
        profit = float(d.get("profit", 0.0) or 0.0)
        pct = max(0.0, min(100.0, (profit / float(max_profit) * 100) if max_profit else 0.0))
        segments.append(
            f"{_a('dim','MaxP')} "
            f"{_a(_pct_color(pct), f'+{max(0.0, profit):.8f}/{float(max_profit):.8f} ({pct:.0f}%)')}"
        )

    max_loss = d.get("max_loss")
    if max_loss is not None:
        profit = float(d.get("profit", 0.0) or 0.0)
        loss_target = abs(float(max_loss))
        loss_now = abs(profit) if profit < 0 else 0.0
        pct = max(0.0, min(100.0, (loss_now / loss_target * 100) if loss_target > 0 else 0.0))
        color = _pct_color(pct) if profit < 0 else "dim"
        segments.append(
            f"{_a('dim','MaxL')} "
            f"{_a(color, f'-{loss_now:.8f}/-{loss_target:.8f} ({pct:.0f}%)')}"
        )

    max_bets = d.get("max_bets")
    if max_bets is not None:
        total = int(d.get("total_bets", 0) or 0)
        pct = max(0.0, min(100.0, (total / int(max_bets) * 100) if max_bets else 0.0))
        segments.append(
            f"{_a('dim','MaxB')} "
            f"{_a(_pct_color(pct), f'{total}/{int(max_bets)} ({pct:.0f}%)')}"
        )

    max_wins = d.get("max_wins")
    if max_wins is not None:
        wins = int(d.get("wins", 0) or 0)
        pct = max(0.0, min(100.0, (wins / int(max_wins) * 100) if max_wins else 0.0))
        segments.append(
            f"{_a('dim','MaxW')} "
            f"{_a(_pct_color(pct), f'{wins}/{int(max_wins)} ({pct:.0f}%)')}"
        )

    floor = d.get("stop_on_balance")
    if floor is not None:
        bal = float(d.get("current_balance", 0.0) or 0.0)
        color = "yellow" if bal <= float(floor) * 1.05 else "dim"
        segments.append(
            f"{_a('dim','MinBal')} {_a(color, f'{bal:.8f}/{float(floor):.8f}')}"
        )

    if d.get("recurring"):
        delay = int(d.get("recurring_delay_sec", 0) or 0)
        segments.append(f"{_a('dim','Rec')} {_a('cyan', f'on({delay}s)')}")

    sr_n = int(d.get("streak_reset_losses") or 0)
    if sr_n:
        sr_delay = int(d.get("streak_reset_delay_sec", 0) or 0)
        segments.append(f"{_a('dim','SR')} {_a('cyan', f'{sr_n}L→{sr_delay}s')}")

    if not segments:
        return f" {_a('dim','CFG')}  {_a('dim','─')}  {_a('dim','no goals set')}"
    return " " + _a("dim", "CFG") + "  " + "  ".join(segments)


def _state_dict_from_botstate() -> dict:
    """Build a status-dict view of `state` for _build_config_progress_row."""
    return {
        "profit": state.profit,
        "profit_threshold": getattr(state, "profit_threshold", None),
        "profit_increment": getattr(state, "profit_increment", None),
        "next_profit_milestone": state.next_profit_milestone,
        "max_profit": getattr(state, "max_profit", None),
        "max_loss": getattr(state, "max_loss", None),
        "max_bets": getattr(state, "max_bets", None),
        "max_wins": getattr(state, "max_wins", None),
        "stop_on_balance": getattr(state, "stop_on_balance", None),
        "recurring": state.recurring,
        "recurring_delay_sec": int(state.recurring_delay),
        "streak_reset_losses": state.streak_reset_losses,
        "streak_reset_delay_sec": int(state.streak_reset_delay),
        "total_bets": state.total_bets,
        "wins": state.wins,
        "current_balance": state.current_balance,
    }


def _metrics_rows() -> list:
    """Two TUI rows: API latency, then efficiency + cycle. No rate-limit row (N/A)."""
    p50, p99 = _compute_api_p50(), _compute_api_p99()
    r1 = (f" {_a('dim', 'API')} {_a('white', f'last {state._last_api_ms:.0f}ms')}"
          f"  {_a('dim', 'p50')} {_a('cyan', f'{p50:.0f}ms')}"
          f"  {_a('dim', 'p99')} {_a('yellow', f'{p99:.0f}ms')}"
          f"  {_a('dim', 'avg')} {_a('white', f'{_compute_api_avg():.0f}ms')}"
          f"  {_a('dim', 'peak')} {_a('red', f'{state.peak_api_ms:.0f}ms')}")
    ot = _compute_on_target_pct()
    if ot is not None:
        r1 += f"  {_a('dim', 'on-target')} {_a('cyan', f'{ot:.0f}%')}"
    eff = _compute_efficiency_pct()
    tmax = _compute_theoretical_max_bps()
    eff_c = "bold_green" if eff >= 90 else "yellow"
    if state.bet_delay > 0:
        basis = f"(max {tmax:.1f} bps @ delay={float(state.bet_delay):.3f}s)"
    else:
        basis = "(raw throughput, no delay)"
    r2 = (f" {_a('dim', 'Efficiency')} {_a(eff_c, f'{eff:.0f}%')} {_a('dim', basis)}"
          f"  {_a('dim', 'Cycle')} {_a('white', f'{state._cycle_ms:.0f}ms/bet')}"
          f" {_a('dim', f'(overhead {state._cycle_overhead_ms:.0f}ms)')}"
          f" {_a('dim', '→')} {_a('magenta', f'{tmax:.1f} bps')}")
    return [r1, r2]


def build_dashboard_screen() -> str:
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h = 24
        term_w = 80

    lines = []

    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")

    def add_blank():
        lines.append(_CLR_EOL)

    # -- Row 1: Header --
    uptime = str(timedelta(seconds=_uptime_sec()))
    _pause_left = _pause_remaining_sec()
    if state.running and _pause_left > 0:
        st_ind = _a("bold_cyan", f"◐ PAUSED {timedelta(seconds=_pause_left)}/"
                                 f"{timedelta(seconds=int(state.session_pause_total))}")
    elif state.running and not state.paused:
        st_ind = _a("bold_green", "* LIVE")
    elif state.paused:
        st_ind = _a("bold_yellow", "* PAUSED")
    else:
        st_ind = _a("bold_red", "* STOPPED")

    game_info = _game_info_str()
    run_seg = f"  Run {fmt_duration(time.time() - state.run_started_at)}"
    add(f"{_a('dim_magenta', '--- ')}{_a('bold_cyan', f'Shuffle v{VERSION}')}"
        f"{_a('white', f'  #{state.session_id}  {uptime}{run_seg}')}  {st_ind} "
        f"{_a('dim', f'  {game_info}')}"
        f"{_a('dim_magenta', ' ' + '-' * max(0, term_w - 80))}")

    # -- Row 2: Balance bar --
    p = state.profit
    ps = "+" if p >= 0 else ""
    pc = "bold_green" if p >= 0 else "bold_red"
    wr = (state.wins / state.total_bets * 100) if state.total_bets else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"

    add(f"{_COLORS['bg_magenta']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{state.current_balance:.8f} {state.currency.upper()}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{p:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{state.wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    # -- Row 3: Stats --
    cs = state.current_streak
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "--")

    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(state.total_bets))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(state.wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(state.losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{state.max_win_streak}/L-{state.max_loss_streak}')}")

    # -- Row 4: Bet / Strategy --
    s4 = (f" {_a('dim', 'Bet')} {_a('yellow', f'{state.current_bet:.8f}')}")
    if state.profit_increment and state.profit_threshold:
        s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{state.base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{state.highest_bet:.8f}')}"
           f"  {_a('cyan', state.strategy)}"
           f"  {_a('dim', f'{state.multiplier_target}x')}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{state.bets_per_second:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{state.bets_per_minute:.0f}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', _fmt_speed())}")
    add(s4)

    # -- Row 5: Extremes --
    hi_bal = state.highest_balance if state.highest_balance > 0 else state.current_balance
    lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
    avg_pnl = state.profit / state.total_bets if state.total_bets else 0.0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = state.current_balance - state.start_balance
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""

    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{state.highest_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{state.biggest_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    # -- Row 6: Sparkline --
    spark = _sparkline(list(state.profit_history))
    sp_c = "green" if p >= 0 else "red"
    add(f" {_a('dim', 'P/L')} {_a(sp_c, spark)}")

    # -- Row 6b (v1.5.0): Config-progress --
    add(_build_config_progress_row(_state_dict_from_botstate()))

    # -- Rows 6c/6d (v1.9.0): API + cycle metrics (WolfBet v2.17.0 / v2.30.3-v2.30.5) --
    for row in _metrics_rows():
        add(row)

    # -- Row 7: Separator --
    add(_a("dim_magenta", "-" * term_w))

    # -- Row 8: Bets table header --
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>10}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # -- Rows 9..N-2: Recent bet rows --
    bets_avail = max(0, term_h - 13)
    recent = list(state.recent_bets)[-bets_avail:] if bets_avail > 0 else []

    for b in reversed(recent):
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if b["pnl"] >= 0 else "red"
        bsign = "+" if b["pnl"] >= 0 else ""
        bn = b["n"]
        btm = b["time"]
        bamt = b["amt"]
        broll = b["roll"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{broll:>10}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    for _ in range(bets_avail - len(recent)):
        add_blank()

    # -- Row N-1: Bottom separator --
    add(_a("dim_magenta", "-" * term_w))

    # -- Row N: Status + controls --
    status_txt = state.status[:60]
    keys = "[P]ause [R]esume [T]weak [Q]uit [H]istory"
    add(f" {_a('white', status_txt)}  {_a('dim', keys)}")

    return "\r\n".join(lines)

# ===========================================================
#  HISTORY SCREEN
# ===========================================================
def show_history():
    was_paused  = state.paused
    state.paused = True
    time.sleep(0.15)

    rows = _db_get_history(15)
    console.clear()
    console.print(Rule("[bold cyan]Session History[/]"))

    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("ID",       width=5,  justify="center")
    t.add_column("Started",  width=17)
    t.add_column("Ended",    width=17)
    t.add_column("Cur",      width=6,  justify="center")
    t.add_column("Game",     width=6,  justify="center")
    t.add_column("Strategy", width=18)
    t.add_column("Bets",     width=7,  justify="right")
    t.add_column("Profit",   width=16, justify="right")
    t.add_column("Wagered",  width=16, justify="right")
    t.add_column("W/L Str",  width=10, justify="center")

    for row in rows:
        sid, started, ended, cur, game, strat, bets, profit, wagered, mws, mls = row
        pc  = "green" if (profit or 0) >= 0 else "red"
        ps  = "+" if (profit or 0) >= 0 else ""
        t.add_row(
            str(sid),
            (started or "").replace("T", " "),
            (ended   or "--").replace("T", " "),
            (cur or "").upper(),
            (game or "limbo"),
            strat or "--",
            str(bets or 0),
            Text(f"{ps}{profit:.8f}" if profit else "0.00000000", style=pc),
            f"{wagered:.8f}" if wagered else "--",
            f"W{mws or 0}/L{mls or 0}",
        )

    console.print(t)
    console.print("\n[dim]  Press any key to return...[/]")
    _getch()
    state.paused = was_paused

def _print_tweak_help():
    """Compact reference of tweakable params with example values."""
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Param",   width=20)
    t.add_column("Example", width=28)
    t.add_column("Notes",   width=32)
    rows = [
        ("delay <sec>",          "delay 0.1",        "bet_delay"),
        ("bbpct <0-1>",          "bbpct 0.001",      "basebet_pct (0 disables)"),
        ("sdloss / sdwin",       "sdloss 3:0.5",     "'N:sec' streak delay"),
        ("sdbets / sbloss",      "sdbets 100:0.5",   "streak-bet reduction"),
        ("maxprofit",            "maxprofit 0.01",   "'off' to disable"),
        ("maxloss / maxbets",    "maxbets 5000",     "instant stop condition"),
        ("maxwins / minbalance", "maxwins 1000",     "instant stop condition"),
        ("msbets / mswins",      "msbets 100",       "milestone thresholds"),
        ("mslosses / msprofit",  "msprofit 0.001",   "milestone thresholds"),
        ("pi / pt",              "pt 0.001",         "profit increment"),
        ("recurring on/off",     "recurring on",     "restart on non-loss stop"),
        ("recdelay",             "recdelay 60",      "pause between cycles (s)"),
        ("srloss / srdelay",     "srloss 20",        "restart on win after N losses"),
        ("basebet <amount>",     "basebet 0.01",     "applies to the next bet"),
        ("multiplier",           "multiplier 3.0",   "dice: target re-derived"),
        ("lossmult / winmult",   "lossmult 2.01",    "strategy multipliers"),
        ("proxy / purgedays",    "proxy off",        "http(s)/socks5 URL; 1-30 days"),
        ("dbmode async/sync",    "dbmode sync",      "writer starts on next run"),
    ]
    for p, ex, note in rows:
        t.add_row(f"[cyan]{p}[/]", f"[yellow]{ex}[/]", f"[dim]{note}[/]")
    console.print(t)


def show_tweak():
    """Modal runtime tweak (WolfBet v2.19.0): pause, read `param value`, apply, resume."""
    was_paused = state.paused
    state.paused = True
    time.sleep(0.15)

    console.clear()
    console.print(Rule("[bold cyan]Runtime tweak[/]"))
    _print_tweak_help()

    while True:
        console.print("\n[dim]Format: [/][cyan]param value[/]  [dim]|  'back' to cancel[/]")
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw or raw.lower() in ("back", "q", "quit"):
            break
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            console.print("  [red]Missing value.[/] Try: [cyan]delay 0.1[/]")
            continue
        param = _TWEAK_ALIAS.get(parts[0].lower(), parts[0].lower())
        if param not in _TWEAK_PARAMS:
            console.print(f"  [red]Unknown param:[/] [cyan]{param}[/]")
            continue
        try:
            msgs = _apply_mutation({param: parts[1]})
        except (ValueError, TypeError) as e:
            console.print(f"  [red]Invalid value:[/] {e}")
            continue
        for m in msgs:
            console.print(f"  [green]✓[/] {m}")
            logger.info("TWEAK: %s", m)
        time.sleep(2.0)
        break

    console.clear()
    state.paused = was_paused

# ===========================================================
#  KEYBOARD INPUT
# ===========================================================
def _getch() -> str:
    try:
        import termios, tty
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return ""

def input_handler():
    while state.running or state.paused:
        ch = _getch().lower()
        if not ch:
            time.sleep(0.1)
            continue
        if ch in ("q", "\x03", "\x1c"):
            state.running  = False
            state.paused   = False
            state.status   = "Stopping -- saving session..."
            logger.warning("SESSION END (user quit)  bets=%d  profit=%+.8f  balance=%.8f",
                        state.total_bets, state.profit, state.current_balance)
            _db_save_session()
        elif ch == "p":
            state.paused = True
            state.status = "PAUSED"
        elif ch == "r":
            state.paused = False
            state.status = "Resumed"
        elif ch == "h":
            show_history()
        elif ch == "t":
            show_tweak()

# ===========================================================
#  SETUP WIZARD
# ===========================================================
_BACK = object()

def _ask(prompt_text: str, default: str = "", choices: list = None) -> str:
    hint = f" [bold yellow]{default}[/]" if default else ""
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    while True:
        try:
            raw = input(": ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if raw.lower() == "back":
            return _BACK
        if not raw:
            if default:
                return default
            if choices:
                console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
                console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
                continue
            return default
        if choices and raw.lower() not in [c.lower() for c in choices]:
            console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
            console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
            continue
        return raw

def _ask_optional(prompt_text: str, current) -> str:
    if current is not None:
        hint = f" [bold yellow]{current}[/]  [dim](type 'none' to clear)[/]"
    else:
        hint = " [dim]none[/]"
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    try:
        raw = input(": ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if raw.lower() == "back":
        return _BACK
    if raw.lower() in ("none", "clear", "off", "disable", "0"):
        return ""
    if not raw:
        return str(current) if current is not None else ""
    return raw


def _build_one_rule():
    console.print("  [bold cyan]-- New Rule --[/]")
    console.print("    [dim]Condition type:[/]")
    for k, name in COND_TYPES.items():
        label = {"sequence": "Sequence (streaks/counts)", "profit": "Profit / Loss / Balance",
                 "bet": "Bet properties"}.get(name, name)
        console.print(f"      [bold yellow][{k}][/] {label}")

    ct = _ask("  Condition type", choices=list(COND_TYPES.keys()))
    if ct is _BACK:
        return _BACK
    cond_type = COND_TYPES[ct]

    cond_field = ""
    cond_mode = ""
    cond_value = 0.0
    cond_trigger = ""

    if cond_type == "sequence":
        console.print("    [dim]When should this fire?[/]")
        for k, (_, label) in SEQ_MODES.items():
            console.print(f"      [bold yellow][{k}][/] {label}")
        sm = _ask("  Sequence mode", choices=list(SEQ_MODES.keys()))
        if sm is _BACK:
            return _BACK
        cond_mode = SEQ_MODES[sm][0]

        nv = _ask("  N value (number)", default="1")
        if nv is _BACK:
            return _BACK
        try:
            cond_value = float(nv)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

        console.print("    [dim]On which event?[/]")
        for k, t in SEQ_TRIGGERS.items():
            console.print(f"      [bold yellow][{k}][/] {t}")
        st = _ask("  Event", choices=list(SEQ_TRIGGERS.keys()))
        if st is _BACK:
            return _BACK
        cond_trigger = SEQ_TRIGGERS[st]

    elif cond_type == "profit":
        console.print("    [dim]Check what?[/]")
        for k, name in PROFIT_FIELDS.items():
            console.print(f"      [bold yellow][{k}][/] {name}")
        pf = _ask("  Field", choices=list(PROFIT_FIELDS.keys()))
        if pf is _BACK:
            return _BACK
        cond_field = PROFIT_FIELDS[pf]

        console.print("    [dim]Comparison:[/]")
        for k, (_, sym) in CMP_OPS.items():
            console.print(f"      [bold yellow][{k}][/] {sym}")
        op = _ask("  Operator", choices=list(CMP_OPS.keys()))
        if op is _BACK:
            return _BACK
        cond_mode = CMP_OPS[op][0]

        vl = _ask(f"  Value ({state.currency.upper()})", default="0")
        if vl is _BACK:
            return _BACK
        try:
            cond_value = float(vl)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    elif cond_type == "bet":
        console.print("    [dim]Check what?[/]")
        for k, name in BET_FIELDS.items():
            console.print(f"      [bold yellow][{k}][/] {name}")
        bf = _ask("  Field", choices=list(BET_FIELDS.keys()))
        if bf is _BACK:
            return _BACK
        cond_field = BET_FIELDS[bf]

        console.print("    [dim]Comparison:[/]")
        for k, (_, sym) in CMP_OPS.items():
            console.print(f"      [bold yellow][{k}][/] {sym}")
        op = _ask("  Operator", choices=list(CMP_OPS.keys()))
        if op is _BACK:
            return _BACK
        cond_mode = CMP_OPS[op][0]

        unit = "%" if cond_field == "winchance" else ("x" if cond_field == "payout" else "")
        vl = _ask(f"  Value{(' (' + unit + ')') if unit else ''}", default="0")
        if vl is _BACK:
            return _BACK
        try:
            cond_value = float(vl)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    console.print("    [dim]Then do what?[/]")
    for k, (_, label) in RULE_ACTIONS.items():
        console.print(f"      [bold yellow][{k:>2}][/] {label}")
    ak = _ask("  Action", choices=list(RULE_ACTIONS.keys()))
    if ak is _BACK:
        return _BACK
    action = RULE_ACTIONS[ak][0]

    action_value = 0.0
    needs_value = action in ("increase_amount", "decrease_amount", "add_amount",
                             "deduct_amount", "set_amount", "set_winchance",
                             "increase_wc", "decrease_wc")
    if needs_value:
        if action in ("increase_amount", "decrease_amount", "increase_wc", "decrease_wc"):
            unit_hint = "%"
        elif action == "set_winchance":
            unit_hint = "% win chance"
        else:
            unit_hint = state.currency.upper()
        av = _ask(f"  Value ({unit_hint})", default="0")
        if av is _BACK:
            return _BACK
        try:
            action_value = float(av)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    rule = StrategyRule(
        cond_type=cond_type, cond_field=cond_field, cond_mode=cond_mode,
        cond_value=cond_value, cond_trigger=cond_trigger,
        action=action, action_value=action_value,
    )
    rule.description = _describe_rule(rule)
    return rule


def _edit_one_rule(old: StrategyRule):
    """Edit an existing rule part-by-part. Returns updated StrategyRule, _BACK, or None."""
    r = StrategyRule.from_dict(old.to_dict())
    act_label = next((v[1] for v in RULE_ACTIONS.values() if v[0] == r.action), r.action)

    while True:
        console.print()
        console.print(f"    [bold]{r.description}[/]")
        console.print()

        parts = []
        if r.cond_type == "sequence":
            mode_label = dict(every="Every N", every_streak="Every streak of N",
                              first_streak="First streak of N",
                              streak_above="Streak above N", streak_below="Streak below N").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Mode",       mode_label))
            parts.append(("N value",    str(int(r.cond_value))))
            parts.append(("Event",      r.cond_trigger))
        elif r.cond_type == "profit":
            op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Field",      r.cond_field))
            parts.append(("Operator",   op_sym))
            parts.append(("Value",      str(r.cond_value)))
        elif r.cond_type == "bet":
            op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Field",      r.cond_field))
            parts.append(("Operator",   op_sym))
            parts.append(("Value",      str(r.cond_value)))

        act_label = next((v[1] for v in RULE_ACTIONS.values() if v[0] == r.action), r.action)
        parts.append(("Action",     act_label))
        if r.action_value:
            parts.append(("Action value", str(r.action_value)))

        for i, (label, val) in enumerate(parts, 1):
            console.print(f"      [bold yellow][{i}][/] {label:14s} [cyan]{val}[/]")
        save_n = len(parts) + 1
        replace_n = save_n + 1
        console.print(f"      [bold yellow][{save_n}][/] [green]Save changes[/]")
        console.print(f"      [bold yellow][{replace_n}][/] [yellow]Rebuild from scratch[/]")

        valid = [str(i) for i in range(1, replace_n + 1)]
        pick = _ask("    Edit which part?", default=str(save_n), choices=valid)
        if pick is _BACK:
            return _BACK
        pick = int(pick)

        if pick == save_n:
            r.description = _describe_rule(r)
            return r

        if pick == replace_n:
            console.print("    [dim]Building replacement rule...[/]")
            replacement = _build_one_rule()
            if replacement is _BACK:
                continue
            return replacement

        label = parts[pick - 1][0]

        if label == "Condition":
            console.print("      [dim]Change condition type:[/]")
            for k, name in COND_TYPES.items():
                tag = {"sequence": "Sequence", "profit": "Profit / Loss / Balance",
                       "bet": "Bet properties"}.get(name, name)
                console.print(f"        [bold yellow][{k}][/] {tag}")
            cur_key = next((k for k, v in COND_TYPES.items() if v == r.cond_type), "1")
            ct = _ask("      Type", default=cur_key, choices=list(COND_TYPES.keys()))
            if ct is _BACK:
                continue
            new_type = COND_TYPES[ct]
            if new_type != r.cond_type:
                r.cond_type = new_type
                if new_type == "sequence":
                    r.cond_field = ""
                    r.cond_mode = "every"
                    r.cond_value = 1.0
                    r.cond_trigger = "win"
                elif new_type == "profit":
                    r.cond_field = "profit"
                    r.cond_mode = "gte"
                    r.cond_value = 0.0
                    r.cond_trigger = ""
                elif new_type == "bet":
                    r.cond_field = "amount"
                    r.cond_mode = "gte"
                    r.cond_value = 0.0
                    r.cond_trigger = ""

        elif label == "Mode":
            console.print("      [dim]Sequence mode:[/]")
            for k, (_, ml) in SEQ_MODES.items():
                console.print(f"        [bold yellow][{k}][/] {ml}")
            cur_key = next((k for k, v in SEQ_MODES.items() if v[0] == r.cond_mode), "1")
            sm = _ask("      Mode", default=cur_key, choices=list(SEQ_MODES.keys()))
            if sm is _BACK:
                continue
            r.cond_mode = SEQ_MODES[sm][0]

        elif label == "N value":
            nv = _ask("      N value", default=str(int(r.cond_value)))
            if nv is _BACK:
                continue
            try:
                r.cond_value = float(nv)
            except ValueError:
                console.print("      [red]Invalid number[/]")

        elif label == "Event":
            console.print("        [bold yellow][1][/] win  [bold yellow][2][/] loss  [bold yellow][3][/] bet")
            cur_key = {"win": "1", "loss": "2", "bet": "3"}.get(r.cond_trigger, "1")
            st = _ask("      Event", default=cur_key, choices=["1", "2", "3"])
            if st is _BACK:
                continue
            r.cond_trigger = SEQ_TRIGGERS[st]

        elif label == "Field":
            if r.cond_type == "profit":
                console.print("        [bold yellow][1][/] profit  [bold yellow][2][/] loss  [bold yellow][3][/] balance")
                cur_key = next((k for k, v in PROFIT_FIELDS.items() if v == r.cond_field), "1")
                pf = _ask("      Field", default=cur_key, choices=list(PROFIT_FIELDS.keys()))
                if pf is _BACK:
                    continue
                r.cond_field = PROFIT_FIELDS[pf]
            elif r.cond_type == "bet":
                for k, name in BET_FIELDS.items():
                    console.print(f"        [bold yellow][{k}][/] {name}")
                cur_key = next((k for k, v in BET_FIELDS.items() if v == r.cond_field), "1")
                bf = _ask("      Field", default=cur_key, choices=list(BET_FIELDS.keys()))
                if bf is _BACK:
                    continue
                r.cond_field = BET_FIELDS[bf]

        elif label == "Operator":
            for k, (_, sym) in CMP_OPS.items():
                console.print(f"        [bold yellow][{k}][/] {sym}")
            cur_key = next((k for k, v in CMP_OPS.items() if v[0] == r.cond_mode), "1")
            op = _ask("      Operator", default=cur_key, choices=list(CMP_OPS.keys()))
            if op is _BACK:
                continue
            r.cond_mode = CMP_OPS[op][0]

        elif label == "Value":
            vl = _ask("      Value", default=str(r.cond_value))
            if vl is _BACK:
                continue
            try:
                r.cond_value = float(vl)
            except ValueError:
                console.print("      [red]Invalid number[/]")

        elif label == "Action":
            console.print("      [dim]Change action:[/]")
            for k, (_, al) in RULE_ACTIONS.items():
                console.print(f"        [bold yellow][{k:>2}][/] {al}")
            cur_key = next((k for k, v in RULE_ACTIONS.items() if v[0] == r.action), "1")
            ak = _ask("      Action", default=cur_key, choices=list(RULE_ACTIONS.keys()))
            if ak is _BACK:
                continue
            new_action = RULE_ACTIONS[ak][0]
            if new_action != r.action:
                r.action = new_action
                r.action_value = 0.0

        elif label == "Action value":
            av = _ask("      Action value", default=str(r.action_value))
            if av is _BACK:
                continue
            try:
                r.action_value = float(av)
            except ValueError:
                console.print("      [red]Invalid number[/]")


def setup_wizard():
    console.clear()
    console.print()
    console.print(Align.center(
        Panel(
            "[bold cyan]Shuffle AutoBot  v1.0[/]\n"
            "[dim]Multi-game auto-bettor with live TUI dashboard[/]",
            box=box.DOUBLE, expand=False, padding=(1, 6), style="magenta"
        )
    ))
    console.print()
    console.print("[dim]  Tips: arrow keys work  |  type 'back' for previous step  |  'none' to clear a value[/]")
    console.print()

    has_saved = load_config()
    saved_token = state.bearer_token

    if has_saved and saved_token:
        console.print("[green]  Found saved config from last session:[/]")
        game_label = GAMES.get(state.game, {}).get("label", state.game)
        console.print(f"    [dim]Game:[/]       [yellow]{game_label}[/]")
        console.print(f"    [dim]Currency:[/]    [yellow]{state.currency.upper()}[/]")
        console.print(f"    [dim]Strategy:[/]    [yellow]{state.strategy}[/]")
        console.print(f"    [dim]Multiplier:[/]  [yellow]{state.multiplier_target}x[/]")
        console.print(f"    [dim]Base bet:[/]    [yellow]{state.base_bet:.8f}[/]")
        console.print(f"    [dim]Token:[/]       [yellow]{saved_token[:12]}...{saved_token[-8:]}[/]")
        console.print()

    def step_auth():
        nonlocal saved_token
        console.print(Rule("[bold cyan]Step 1 / 7 -- Authentication[/]"))
        console.print("  [dim]You need the Bearer token from browser DevTools (F12 -> Network tab):[/]")
        console.print("    [yellow]1.[/] Open shuffle.com and log in")
        console.print("    [yellow]2.[/] Open DevTools -> Network tab")
        console.print("    [yellow]3.[/] Click any GraphQL request")
        console.print("    [yellow]4.[/] Copy the Authorization header value (starts with 'Bearer eyJ...')")
        console.print()

        if saved_token:
            console.print(f"  Saved token: [yellow]{saved_token[:12]}...{saved_token[-8:]}[/]")
            v = _ask("Use saved credentials? (y/n)", default="y", choices=["y", "n"])
            if v is _BACK:
                return False
            if v.lower() == "y":
                state.bearer_token = saved_token
            else:
                v = _ask("Paste your Bearer token (without 'Bearer ' prefix)")
                if v is _BACK:
                    return False
                # Strip "Bearer " prefix if user pastes the full header
                if v.lower().startswith("bearer "):
                    v = v[7:]
                state.bearer_token = v
                saved_token = v
        else:
            v = _ask("Paste your Bearer token (without 'Bearer ' prefix)")
            if v is _BACK:
                return False
            if v.lower().startswith("bearer "):
                v = v[7:]
            state.bearer_token = v
            saved_token = v

        return True

    def step_game():
        console.print(Rule("[bold cyan]Step 2 / 7 -- Game Selection[/]"))
        game_keys = list(GAMES.keys())
        for i, gk in enumerate(game_keys, 1):
            g = GAMES[gk]
            console.print(f"    [bold yellow][{i}][/] [cyan]{g['label']}[/]")
        console.print()

        choices = [str(i) for i in range(1, len(game_keys) + 1)]
        cur_idx = game_keys.index(state.game) + 1 if state.game in game_keys else 1
        v = _ask("Select game", default=str(cur_idx), choices=choices)
        if v is _BACK:
            return False
        state.game = game_keys[int(v) - 1]

        # Test connection with this game
        console.print(f"  [dim]Testing connection ({GAMES[state.game]['label']})...[/]")
        try:
            api_test_connection()
            console.print("[green]  Connected![/]\n")
        except Exception as e:
            detail = f": {e}" if APP_ENV != "production" else ""
            console.print(f"[red]  Connection failed{detail}.[/]")
            console.print("[yellow]  Check your token and try again.[/]\n")
            state.bearer_token = ""
            return False
        return True

    def step_currency():
        console.print(Rule("[bold cyan]Step 3 / 7 -- Currency & Balance[/]"))

        console.print("  [dim]Fetching balances from Shuffle.com...[/]")
        balances = api_get_balances()

        if balances:
            console.print("  [green]Balances found:[/]")
            bal_map = {}
            for b in balances:
                cur = b["currency"].lower()
                amt = b["amount"]
                bal_map[cur] = amt
                console.print(f"    [yellow]{cur.upper():>6}[/]  {amt:.8f}")
            console.print()

            v = _ask("Select currency", default=state.currency, choices=CURRENCIES)
            if v is _BACK:
                return False
            state.currency = v.lower()

            if state.currency in bal_map:
                bal = bal_map[state.currency]
                console.print(f"  [green]Using API balance: {bal:.8f} {state.currency.upper()}[/]")
                v = _ask("Accept this balance? (or enter a different one)",
                         default=str(bal))
            else:
                console.print(f"  [yellow]No {state.currency.upper()} balance found on API.[/]")
                v = _ask(f"Enter your current {state.currency.upper()} balance",
                         default=str(state.start_balance) if state.start_balance else "")
        else:
            console.print("  [yellow]Could not fetch balances from API.[/]")
            console.print("  [dim]Enter your balance manually (check on shuffle.com).[/]")
            console.print()

            v = _ask("Select currency", default=state.currency, choices=CURRENCIES)
            if v is _BACK:
                return False
            state.currency = v.lower()

            v = _ask(f"Enter your current {state.currency.upper()} balance",
                     default=str(state.start_balance) if state.start_balance else "")

        if v is _BACK:
            return False
        try:
            bal = float(v)
        except ValueError:
            console.print("    [red]Enter a valid number[/]")
            return False
        state.start_balance   = bal
        state.current_balance = bal
        state.highest_balance = bal
        state.lowest_balance  = bal
        console.print(f"  [green]Balance: {bal:.8f} {state.currency.upper()}[/]\n")
        return True

    def step_bet_config():
        console.print(Rule("[bold cyan]Step 4 / 7 -- Bet Configuration[/]"))
        game_label = GAMES[state.game]["label"]

        if state.game == "limbo":
            console.print(f"  [dim]Game: {game_label} -- pick a multiplier target, win if result >= target[/]")
            console.print()
            console.print("  [dim]Multiplier examples (Limbo):[/]")
            for mult, chance in [(1.5, 66.0), (2.0, 49.5), (3.0, 33.0), (5.0, 19.8), (10.0, 9.9), (100.0, 0.99)]:
                console.print(f"    [yellow]{mult:6.1f}x[/]  ->  ~{chance:.1f}% win")
            console.print()

            while True:
                v = _ask("Multiplier target", default=str(state.multiplier_target))
                if v is _BACK:
                    return False
                try:
                    m = float(v)
                except ValueError:
                    console.print("    [red]Enter a valid number[/]")
                    continue
                if m < 1.01:
                    console.print("    [red]Multiplier must be at least 1.01[/]")
                    continue
                state.multiplier_target = m
                break
            wc = 99.0 / state.multiplier_target
            console.print(f"    [dim]-> Win chance:[/] [yellow]{wc:.2f}%[/]\n")

        elif state.game == "dice":
            console.print(f"  [dim]Game: {game_label} -- roll above/below a target number (0-100)[/]")
            console.print()

            v = _ask("Bet direction", default=state.dice_condition, choices=["above", "below"])
            if v is _BACK:
                return False
            state.dice_condition = v.lower()

            console.print()
            console.print("  [dim]Multiplier examples (Dice):[/]")
            for mult in [2.0, 3.0, 5.0, 10.0, 1.5]:
                wc = 99.0 / mult
                if state.dice_condition == "above":
                    tgt = round(100.0 - wc, 2)
                else:
                    tgt = round(wc, 2)
                console.print(f"    [yellow]{mult:5.2f}x[/]  target={tgt:6.2f}  ~{wc:.1f}% win")
            console.print()

            while True:
                v = _ask("Multiplier", default=str(state.multiplier_target))
                if v is _BACK:
                    return False
                try:
                    m = float(v)
                except ValueError:
                    console.print("    [red]Enter a valid number[/]")
                    continue
                if m < 1.01:
                    console.print("    [red]Multiplier must be at least 1.01[/]")
                    continue
                state.multiplier_target = m
                break

            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
            console.print(f"    [dim]-> Target:[/] [yellow]{state.dice_condition} {state.dice_target}[/]"
                          f"  [dim]Win chance:[/] [yellow]{wc:.2f}%[/]\n")

        console.print(f"  [dim]Minimum bet: {MIN_BET} | Use 0 for test bets (no real money)[/]")
        v = _ask(f"Base bet amount ({state.currency.upper()})", default=str(state.base_bet))
        if v is _BACK:
            return False
        bet = float(v)
        if bet > 0 and bet < MIN_BET:
            console.print(f"    [yellow]Rounding up to minimum: {MIN_BET}[/]")
            bet = MIN_BET
        state.base_bet    = bet
        state.current_bet = state.base_bet
        return True

    def step_strategy():
        console.print(Rule("[bold cyan]Step 5 / 7 -- Betting Strategy[/]"))
        for k, (name, desc) in STRATEGIES.items():
            console.print(f"    [bold yellow][{k}][/] [cyan]{name:20s}[/]  [dim]{desc}[/]")
        console.print()

        v = _ask("Select strategy", default=state.strategy_key, choices=list(STRATEGIES.keys()))
        if v is _BACK:
            return False
        state.strategy_key = v
        state.strategy     = STRATEGIES[v][0]

        if v in ("2", "3", "5", "6"):
            console.print()
            if v in ("2", "6"):
                console.print(f"  [dim]On loss: multiply bet by[/] [yellow]{state.loss_mult}x[/]")
                lm = _ask("  Loss multiplier", default=str(state.loss_mult))
                if lm is _BACK:
                    return False
                try:
                    state.loss_mult = float(lm)
                except ValueError:
                    pass
            if v in ("3", "5"):
                console.print(f"  [dim]On win: multiply bet by[/] [yellow]{state.win_mult}x[/]")
                wm = _ask("  Win multiplier", default=str(state.win_mult))
                if wm is _BACK:
                    return False
                try:
                    state.win_mult = float(wm)
                except ValueError:
                    pass
            console.print()

        if v == "6":
            console.print()
            console.print("  [bold cyan]Delay Martingale[/] -- flat bet through the first N")
            console.print("  consecutive losses, then start doubling.")
            console.print()
            d = _ask("  Losses before doubling starts", default=str(state.delay_martin_threshold))
            if d is _BACK:
                return False
            state.delay_martin_threshold = int(d)
            console.print(f"    [green]-> Flat for {state.delay_martin_threshold} losses, then Martingale[/]\n")

        elif v == "7":
            console.print()
            console.print("  [bold cyan]Rule-Based Strategy[/] -- build custom conditions & actions")
            console.print("  [dim]Each rule: IF <condition> THEN <action>[/]")
            console.print()

            while True:
                if state.custom_rules:
                    console.print(f"  [yellow]Current rules ({len(state.custom_rules)}):[/]")
                    for i, r in enumerate(state.custom_rules, 1):
                        console.print(f"    [dim]{i}.[/] {r.description}")
                    console.print()
                    console.print("    [bold yellow][1][/] Add rule   [bold yellow][2][/] Edit rule   [bold yellow][3][/] Delete rule   [bold yellow][4][/] Clear all   [bold yellow][5][/] Continue")
                    v2 = _ask("  Select", default="5", choices=["1", "2", "3", "4", "5"])
                    if v2 is _BACK:
                        return False
                    if v2 == "5":
                        break
                    elif v2 == "1":
                        rule = _build_one_rule()
                        if rule is _BACK:
                            continue
                        if rule is not None:
                            state.custom_rules.append(rule)
                            console.print(f"    [green]+ Rule {len(state.custom_rules)}:[/] {rule.description}\n")
                    elif v2 == "2":
                        if len(state.custom_rules) == 1:
                            idx = 0
                        else:
                            en = _ask(f"  Rule # to edit (1-{len(state.custom_rules)})")
                            if en is _BACK:
                                continue
                            try:
                                idx = int(en) - 1
                                if not (0 <= idx < len(state.custom_rules)):
                                    console.print("    [red]Invalid rule number[/]")
                                    continue
                            except ValueError:
                                console.print("    [red]Enter a number[/]")
                                continue
                        old = state.custom_rules[idx]
                        console.print(f"    [dim]Editing:[/] {old.description}")
                        replacement = _edit_one_rule(old)
                        if replacement is _BACK:
                            continue
                        if replacement is not None:
                            state.custom_rules[idx] = replacement
                            console.print(f"    [green]Updated:[/] {replacement.description}\n")
                    elif v2 == "3":
                        if len(state.custom_rules) == 1:
                            idx = 0
                        else:
                            en = _ask(f"  Rule # to delete (1-{len(state.custom_rules)})")
                            if en is _BACK:
                                continue
                            try:
                                idx = int(en) - 1
                                if not (0 <= idx < len(state.custom_rules)):
                                    console.print("    [red]Invalid rule number[/]")
                                    continue
                            except ValueError:
                                console.print("    [red]Enter a number[/]")
                                continue
                        removed = state.custom_rules.pop(idx)
                        console.print(f"    [red]Deleted:[/] {removed.description}\n")
                    elif v2 == "4":
                        state.custom_rules = []
                        console.print("    [yellow]All rules cleared.[/]\n")
                else:
                    console.print("  [dim]No rules yet. Let's build some.[/]\n")
                    rule = _build_one_rule()
                    if rule is _BACK:
                        return False
                    if rule is not None:
                        state.custom_rules.append(rule)
                        console.print(f"    [green]+ Rule {len(state.custom_rules)}:[/] {rule.description}\n")
                    else:
                        break

            if state.custom_rules:
                state.custom_rules_text = json.dumps([r.to_dict() for r in state.custom_rules])
                console.print(f"  [bold green]-> {len(state.custom_rules)} rule(s) configured[/]\n")
            else:
                console.print("  [yellow]No rules configured -- will use Flat bet.[/]\n")

        console.print()
        return True

    def step_advanced_tuning():
        """v1.9.0 — optional session tuning (WolfBet v2.18.0 Step 5b)."""
        console.print(Rule("[bold cyan]Step 5b / 7 -- Advanced session tuning (optional)[/]"))
        tuned = (state.recurring or state.streak_reset_losses or state.basebet_pct
                 or state.streak_delay_loss[0] or state.streak_delay_win[0]
                 or state.streak_delay_bets[0] or state.streakbet_loss[0])
        gate = _ask("Configure advanced tuning? (y/N)", default="y" if tuned else "n",
                    choices=["y", "n"])
        if gate is _BACK:
            return False
        if gate.lower() != "y":
            console.print()
            return True

        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        rec = _ask("Recurring restart on non-loss stop? (y/N)",
                   default="y" if state.recurring else "n", choices=["y", "n"])
        if rec is _BACK:
            return False
        state.recurring = rec.lower() == "y"

        v = _ask_optional("Pause between recurring cycles in seconds (0 = back-to-back)",
                          state.recurring_delay if state.recurring_delay > 0 else None)
        if v is _BACK:
            return False
        try:
            state.recurring_delay = max(0.0, min(float(RECURRING_DELAY_MAX), float(v))) if v else 0.0
        except ValueError:
            state.recurring_delay = 60.0

        v = _ask_optional("Restart session on first win after N losses in a row (blank/none = off)",
                          state.streak_reset_losses or None)
        if v is _BACK:
            return False
        try:
            state.streak_reset_losses = max(1, int(v)) if v else 0
        except ValueError:
            state.streak_reset_losses = 0
        if state.streak_reset_losses:
            v = _ask_optional("Pause before streak-reset restart in seconds",
                              state.streak_reset_delay if state.streak_reset_delay > 0 else None)
            if v is _BACK:
                return False
            try:
                state.streak_reset_delay = max(0.0, min(float(RECURRING_DELAY_MAX), float(v))) if v else 0.0
            except ValueError:
                state.streak_reset_delay = 600.0

        v = _ask_optional("Base bet as % of balance (0-1, blank/none to disable)",
                          state.basebet_pct or None)
        if v is _BACK:
            return False
        try:
            state.basebet_pct = float(v) if v else 0.0
        except ValueError:
            state.basebet_pct = 0.0

        for attr, prompt in [
            ("streak_delay_loss", "Streak delay on losses (format 'N:sec')"),
            ("streak_delay_win",  "Streak delay on wins (format 'N:sec')"),
            ("streak_delay_bets", "Streak delay every N bets (format 'N:sec')"),
            ("streakbet_loss",    "Streakbet loss reduction (format 'N:multiplier')"),
        ]:
            tup = getattr(state, attr)
            raw = _ask_optional(prompt, f"{tup[0]}:{tup[1]:g}" if tup[0] > 0 else None)
            if raw is _BACK:
                return False
            setattr(state, attr, parse_streak_delay(raw))   # (0, 0.0) on empty / malformed

        v = _ask_optional("Milestone every N bets (default 100)", state.milestone_bets or 100)
        if v is _BACK:
            return False
        try:
            state.milestone_bets = int(v) if v else 100
        except ValueError:
            state.milestone_bets = 100

        for attr, prompt in [("milestone_wins",   "Milestone every N wins"),
                             ("milestone_losses", "Milestone every N losses")]:
            v = _ask_optional(prompt, getattr(state, attr) or None)
            if v is _BACK:
                return False
            try:
                setattr(state, attr, int(v) if v else 0)
            except ValueError:
                setattr(state, attr, 0)

        v = _ask_optional(f"Milestone every {state.currency.upper()} profit crossed",
                          state.milestone_profit or None)
        if v is _BACK:
            return False
        try:
            state.milestone_profit = float(v) if v else 0.0
        except ValueError:
            state.milestone_profit = 0.0

        console.print()
        return True

    def step_stop_conditions():
        console.print(Rule("[bold cyan]Step 6 / 7 -- Stop Conditions[/]"))
        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        v = _ask_optional(f"Stop when profit reaches ({state.currency.upper()})", state.max_profit)
        if v is _BACK:
            return False
        state.max_profit = float(v) if v else None

        v = _ask_optional(f"Stop when loss exceeds ({state.currency.upper()})", state.max_loss)
        if v is _BACK:
            return False
        state.max_loss = float(v) if v else None

        v = _ask_optional("Stop after N bets", state.max_bets)
        if v is _BACK:
            return False
        state.max_bets = int(v) if v else None

        v = _ask_optional("Stop after N wins", state.max_wins)
        if v is _BACK:
            return False
        state.max_wins = int(v) if v else None

        v = _ask_optional(f"Stop if balance drops below ({state.currency.upper()})", state.stop_on_balance)
        if v is _BACK:
            return False
        state.stop_on_balance = float(v) if v else None

        console.print("\n  [bold]Profit-based bet increment[/] [dim]-- auto-raise base bet as profit grows[/]")
        v = _ask_optional(f"Profit threshold to trigger increment ({state.currency.upper()})", state.profit_threshold)
        if v is _BACK:
            return False
        state.profit_threshold = float(v) if v else None

        v = _ask_optional(f"Increment amount to add to base bet ({state.currency.upper()})", state.profit_increment)
        if v is _BACK:
            return False
        state.profit_increment = float(v) if v else None

        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold

        console.print()
        return True

    def step_confirm():
        console.print(Rule("[bold green]Step 7 / 7 -- Review & Confirm[/]"))
        game_label = GAMES[state.game]["label"]
        wc = _get_win_chance()

        rows = [
            ("Platform",         f"Shuffle.com -- {game_label}"),
            ("Currency",         f"{state.currency.upper()}"),
            ("Balance",          f"{state.start_balance:.8f}"),
        ]
        if state.game == "limbo":
            rows.append(("Multiplier", f"{state.multiplier_target}x  (win chance {wc:.2f}%)"))
        elif state.game == "dice":
            rows.append(("Dice target", f"{state.dice_condition} {state.dice_target}"))
            rows.append(("Multiplier",  f"{state.multiplier_target}x  (win chance {wc:.2f}%)"))
        rows += [
            ("Base bet",         f"{state.base_bet:.8f} {state.currency.upper()}" +
                                 (" [TEST MODE]" if state.base_bet == 0 else "")),
            ("Strategy",         state.strategy),
        ]
        if state.strategy_key == "6":
            rows.append(("Delay",      f"flat for {state.delay_martin_threshold} losses"))
        if state.strategy_key == "7" and state.custom_rules:
            rows.append(("Rules",      f"{len(state.custom_rules)} custom rule(s)"))
        rows += [
            ("Max profit",       str(state.max_profit) if state.max_profit else "--"),
            ("Max loss",         str(state.max_loss)   if state.max_loss   else "--"),
            ("Max bets",         str(state.max_bets)   if state.max_bets   else "Unlimited"),
            ("Max wins",         str(state.max_wins)   if state.max_wins   else "Unlimited"),
            ("Min balance",      str(state.stop_on_balance) if state.stop_on_balance else "--"),
            ("Profit increment", f"+{state.profit_increment:.8f} every {state.profit_threshold} profit"
                if state.profit_increment and state.profit_threshold else "--"),
        ]
        for k, val in rows:
            console.print(f"  [dim]{k:18s}[/] [yellow]{val}[/]")

        if state.strategy_key == "7" and state.custom_rules:
            console.print()
            for i, r in enumerate(state.custom_rules, 1):
                console.print(f"    [dim]{i}.[/] [green]{r.description}[/]")

        risky = _check_risky_config()
        if risky:
            console.print()
            console.print(Panel("\n".join(f"• {w}" for w in risky),
                                title="[bold yellow]Heads-up — review before starting[/]",
                                border_style="yellow", expand=False))

        console.print()
        if state.base_bet == 0:
            console.print("[bold cyan]  TEST MODE: bets cost nothing, no real money wagered.[/]")
        else:
            console.print("[bold red]  WARNING: Real money will be wagered. Bet responsibly.[/]")
        console.print()

        v = _ask("Start auto-betting? (y/n/back)", default="n", choices=["y", "n", "back"])
        if v is _BACK or v.lower() == "back":
            return False
        if v.lower() != "y":
            console.print("[yellow]Aborted.[/]")
            sys.exit(0)

        save_config()
        console.print(f"[dim]  Config saved to {CONFIG_PATH}[/]\n")
        return True

    steps = [step_auth, step_game, step_currency, step_bet_config, step_strategy,
             step_advanced_tuning, step_stop_conditions, step_confirm]
    i = 0
    while i < len(steps):
        ok = steps[i]()
        if ok:
            i += 1
        else:
            i = max(0, i - 1)

# ===========================================================
#  PRINT SESSION SUMMARY
# ===========================================================
def _print_summary():
    console.print()
    console.print(Rule("[bold cyan]Session Complete[/]"))
    console.print()
    if state._bets_this_sec > 0:
        if state._bets_this_sec > state.peak_bps:
            state.peak_bps = state._bets_this_sec
        if state._bets_this_sec < state.low_bps:
            state.low_bps = state._bets_this_sec
    if state._bets_this_min > 0:
        if state._bets_this_min > state.peak_bpm:
            state.peak_bpm = state._bets_this_min
        if state._bets_this_min < state.low_bpm:
            state.low_bpm = state._bets_this_min

    lo_bps = int(state.low_bps) if state.low_bps != float("inf") else 0
    lo_bpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0

    game_label = GAMES.get(state.game, {}).get("label", state.game)
    pairs = [
        ("Session ID",      str(state.session_id)),
        ("Platform",        f"Shuffle.com -- {game_label}"),
        ("Total Bets",      str(state.total_bets)),
        ("Wins / Losses",   f"{state.wins} / {state.losses}"),
        ("Win Rate",        f"{state.wins/state.total_bets*100:.1f}%" if state.total_bets else "--"),
        ("Profit",          f"{state.profit:+.8f} {state.currency.upper()}"),
        ("Start Balance",   f"{state.start_balance:.8f} {state.currency.upper()}"),
        ("Final Balance",   f"{state.current_balance:.8f} {state.currency.upper()}"),
        ("Streaks",         f"W+{state.max_win_streak}  L-{state.max_loss_streak}"),
        ("Best Win",        f"+{state.highest_win:.8f}"),
        ("Worst Loss",      f"-{state.biggest_loss:.8f}"),
        ("Speed",           f"{state.bets_per_second:.1f}/s  {state.bets_per_minute:.0f}/m  "
                            f"Range: {lo_bps}-{int(state.peak_bps)}/s  "
                            f"{lo_bpm}-{int(state.peak_bpm)}/m"),
    ]
    for k, v in pairs:
        sty = "green" if ("profit" in k.lower() and state.profit >= 0) else (
              "red"   if ("profit" in k.lower() and state.profit <  0) else "white")
        console.print(f"  [dim]{k:18s}[/] [{sty}]{v}[/]")
    console.print()

# ===========================================================
#  CLI COMMANDS
# ===========================================================
def cmd_stats(since: str = None, until: str = None, lane: int = None):
    """Show session statistics, optionally filtered to a date window and lane."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    init_db()  # v1.8.0 — adds sessions.lane on DBs the bot hasn't opened yet
    try:
        where, params, window_label = stats_date_window(since, until, lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return
    conn = _db_conn()
    limit = 50 if (since or until) else 20

    rows = conn.execute(f"""
        SELECT id, started_at AS started, ended_at AS ended,
               UPPER(currency) AS cur, COALESCE(game, 'limbo') AS game,
               strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
               COALESCE(lane, 1)
        FROM sessions{where} ORDER BY id DESC LIMIT ?
    """, (*params, limit)).fetchall()

    if not rows:
        console.print("[dim]No sessions found.[/]")
        conn.close()
        return

    console.print(Rule(f"[bold cyan]Session History (last {limit})[/]{window_label}"))

    for r in rows:
        sid, started, ended, cur, game, strat, mult, base, bets, wins, losses, \
            profit, wagered, start_bal, end_bal, mws, mls, \
            hi_bal, lo_bal, hi_win, big_loss, \
            bpm, bps, s_peak_bps, s_low_bps, s_peak_bpm, s_low_bpm, s_lane = r
        lane_tag = f" [magenta]\\[L{s_lane}][/]" if s_lane and s_lane > 1 else ""
        bets = bets or 0
        wins = wins or 0
        losses = losses or 0
        profit = profit or 0
        wagered = wagered or 0
        start_bal = start_bal or 0
        end_bal = end_bal or 0
        mws = mws or 0
        mls = mls or 0
        mult = mult or 0
        base = base or 0
        hi_bal = hi_bal or 0
        lo_bal = lo_bal or 0
        hi_win = hi_win or 0
        big_loss = big_loss or 0
        bpm = bpm or 0
        bps = bps or 0
        s_peak_bps = int(s_peak_bps or 0)
        s_low_bps = int(s_low_bps or 0)
        s_peak_bpm = int(s_peak_bpm or 0)
        s_low_bpm = int(s_low_bpm or 0)
        pc = "green" if profit >= 0 else "red"
        ps = "+" if profit >= 0 else ""
        wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"
        avg_pnl = profit / bets if bets > 0 else 0
        avg_c = "green" if avg_pnl >= 0 else "red"
        avg_s = "+" if avg_pnl >= 0 else ""
        game_label = GAMES.get(game, {}).get("label", game or "?")

        uptime_str = ""
        try:
            t0 = datetime.fromisoformat(started) if started else None
            t1 = datetime.fromisoformat(ended) if ended else datetime.now()
            if t0:
                delta = int((t1 - t0).total_seconds())
                h, rem = divmod(max(delta, 0), 3600)
                m, s = divmod(rem, 60)
                uptime_str = f"  [dim]Uptime:[/] {h}h {m}m {s}s"
        except Exception:
            pass

        console.print(f"  [bold cyan]Session #{sid}[/]{lane_tag}  [dim]{(started or '?').replace('T', ' ')} -> {(ended or 'running').replace('T', ' ')}[/]{uptime_str}")
        console.print(f"    [dim]Game:[/] {game_label}  [dim]Currency:[/] {cur or '?'}  [dim]Strategy:[/] {strat or '?'}  [dim]Mult:[/] {mult}x  [dim]Base:[/] {base:.8f}")
        console.print(f"    [dim]Bets:[/] {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
        console.print(f"    [dim]Speed:[/] {bps:.1f} avg/s  {bpm:.0f} avg/m  [dim]BPS:[/] {s_low_bps}-{s_peak_bps}/s  [dim]BPM:[/] {s_low_bpm}-{s_peak_bpm}/m")
        console.print(f"    [dim]Profit:[/] [{pc}]{ps}{profit:.8f}[/]  [dim]Wagered:[/] {wagered:.8f}  [dim]Avg P/L:[/] [{avg_c}]{avg_s}{avg_pnl:.8f}[/]")
        console.print(f"    [dim]Balance:[/] {start_bal:.8f} -> {end_bal:.8f}  [dim]Peak:[/] [green]{hi_bal:.8f}[/]  [dim]Low:[/] [red]{lo_bal:.8f}[/]")
        console.print(f"    [dim]Streaks:[/] W+{mws}  L-{mls}  [dim]Best Win:[/] [green]+{hi_win:.8f}[/]  [dim]Worst Loss:[/] [red]-{big_loss:.8f}[/]")
        console.print()

    # totals — honor the same date window (and lane) as the listing
    span_first, span_last, span_sec, bet_sec = sessions_time_span(conn, where, params)
    totals = conn.execute(f"""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
               COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
               COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
               COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
               COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
               COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
               COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
               COALESCE(AVG(bets_per_second),0),
               COALESCE(MAX(wins),0), COALESCE(AVG(wins),0),
               COALESCE(MAX(losses),0), COALESCE(AVG(losses),0)
        FROM sessions{where}
    """, tuple(params)).fetchone()
    conn.close()

    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered, \
        best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets, \
        all_hi_bal, all_hi_win, all_big_loss, avg_bpm, \
        all_peak_bps, all_peak_bpm, avg_bps, \
        max_wins_s, avg_wins_s, max_losses_s, avg_losses_s = totals
    pc = "green" if tot_profit >= 0 else "red"
    ps = "+" if tot_profit >= 0 else ""
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "—"
    bp_c = "green" if best_profit >= 0 else "red"
    wp_c = "green" if worst_profit >= 0 else "red"
    avg_pnl = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_pc = "green" if avg_pnl >= 0 else "red"
    avg_ps = "+" if avg_pnl >= 0 else ""

    console.print(Rule(f"[bold cyan]{'Range' if (since or until or lane) else 'All-Time'} Totals[/]{window_label}"))
    rng = (f"  [dim]({span_first.isoformat(' ', 'seconds')} → "
           f"{span_last.isoformat(' ', 'seconds')})[/]") if span_first else ""
    console.print(f"  [dim]Sessions:[/]      {sessions}{rng}")
    console.print(f"  [dim]Time Span:[/]     {fmt_duration(span_sec)}   "
                  f"[dim]Bet Time:[/] {fmt_duration(bet_sec)}")
    console.print(f"  [dim]Total Bets:[/]    {tot_bets}  (W {tot_wins} / L {tot_losses}  WR {wr})")
    console.print(f"  [dim]Wagered:[/]       {tot_wagered:.8f}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{tot_profit:.8f}[/]")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_pc}]{avg_ps}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Best Session:[/]  [{bp_c}]+{best_profit:.8f}[/]")
    console.print(f"  [dim]Worst Session:[/] [{wp_c}]{worst_profit:.8f}[/]")
    console.print(f"  [dim]Peak Balance:[/]  {all_hi_bal:.8f}")
    console.print(f"  [dim]Best Win:[/]      [green]+{all_hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{all_big_loss:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{best_ws}  L-{best_ls}")
    console.print(f"  [dim]Speed:[/]         {avg_bps:.1f} avg/s  {avg_bpm:.0f} avg/m  [dim]Peak:[/] {int(all_peak_bps)}/s  {int(all_peak_bpm)}/m")
    console.print(f"  [dim]Bets/Session:[/]  Max {max_bets}  Avg {avg_bets:.0f}")
    console.print(f"  [dim]Wins/Session:[/]  Max {max_wins_s}  Avg {avg_wins_s:.1f}")
    console.print(f"  [dim]Losses/Session:[/] Max {max_losses_s}  Avg {avg_losses_s:.1f}")
    console.print()


# ===========================================================
#  ANALYTICS (WolfBet v2.23.0 / v2.31.0 / v2.34.0 / v2.35.0)
# ===========================================================
_ANALYTICS_COLS = ("sessions", "bets", "wins", "losses", "profit", "wagered")
_ANALYTICS_AGG = """COUNT(*)                     AS sessions,
                   COALESCE(SUM(total_bets), 0) AS bets,
                   COALESCE(SUM(wins), 0)       AS wins,
                   COALESCE(SUM(losses), 0)     AS losses,
                   COALESCE(SUM(profit), 0)     AS profit,
                   COALESCE(SUM(wagered), 0)    AS wagered"""


def _analytics_lifetime(where: str = "", params=()) -> dict:
    """Aggregate totals over the sessions in the window."""
    conn = _db_conn()
    try:
        r = conn.execute(f"""
            SELECT COUNT(*),
                   COALESCE(SUM(total_bets), 0), COALESCE(SUM(wins), 0),
                   COALESCE(SUM(losses), 0), COALESCE(SUM(profit), 0),
                   COALESCE(SUM(wagered), 0),
                   COALESCE(MAX(profit), 0), COALESCE(MIN(profit), 0),
                   COALESCE(SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END), 0)
            FROM sessions{where}
        """, tuple(params)).fetchone()
    finally:
        conn.close()
    sessions, bets, wins, losses, profit, wagered, best, worst, in_p, in_l = r
    return {
        "sessions_total": sessions, "bets_total": bets,
        "wins_total": wins, "losses_total": losses,
        "profit_total": profit, "wagered_total": wagered,
        "best_profit": best, "worst_profit": worst,
        "sessions_in_profit": in_p, "sessions_in_loss": in_l,
        "win_rate_pct": (wins / bets * 100) if bets else 0.0,
    }


def _analytics_group(key_sql: str, key_name: str, group_sql: str, order_sql: str,
                     where: str = "", params=()) -> list:
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT {key_sql} AS {key_name},
                   {_ANALYTICS_AGG}
            FROM sessions{where}
            GROUP BY {group_sql}
            ORDER BY {order_sql}
        """, tuple(params)).fetchall()
    finally:
        conn.close()
    return [dict(zip((key_name,) + _ANALYTICS_COLS, r)) for r in rows]


def _analytics_by_game(where: str = "", params=()) -> list:
    """Sessions grouped by game, labelled with the game's display name."""
    rows = _analytics_group("COALESCE(game, 'limbo')", "game",
                            "COALESCE(game, 'limbo')", "profit DESC", where, params)
    for r in rows:
        r["game"] = GAMES.get(r["game"], {}).get("label", r["game"])
    return rows


def _analytics_by_strategy(where: str = "", params=()) -> list:
    return _analytics_group("COALESCE(strategy, '?')", "strategy",
                            "strategy", "profit DESC", where, params)


def _analytics_by_lane(where: str = "", params=()) -> list:
    """v2.35.0 — sessions grouped by parallel lane (old rows count as lane 1)."""
    return _analytics_group("'Lane ' || COALESCE(lane, 1)", "lane",
                            "COALESCE(lane, 1)", "COALESCE(lane, 1)", where, params)


def _analytics_top_sessions(order: str = "profit_desc", limit: int = 5,
                            where: str = "", params=()) -> list:
    """Top-N sessions by a whitelisted order key."""
    order_sql = {
        "profit_desc": "profit DESC",
        "profit_asc":  "profit ASC",
        "bets_desc":   "total_bets DESC",
    }.get(order, "profit DESC")
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT id, COALESCE(game, 'limbo'), COALESCE(strategy, '?'),
                   COALESCE(total_bets, 0), COALESCE(wins, 0), COALESCE(losses, 0),
                   COALESCE(profit, 0), COALESCE(wagered, 0), started_at
            FROM sessions{where}
            ORDER BY {order_sql}
            LIMIT ?
        """, (*params, limit)).fetchall()
    finally:
        conn.close()
    out = [dict(zip(("id", "game", "strategy", "bets", "wins", "losses",
                     "profit", "wagered", "started_at"), r)) for r in rows]
    for r in out:
        r["game"] = GAMES.get(r["game"], {}).get("label", r["game"])
    return out


def _print_analytics_table(title: str, rows: list, key_attr: str, key_label: str):
    if not rows:
        return
    t = Table(title=title, box=box.SIMPLE, expand=True)
    t.add_column(key_label, style="cyan")
    t.add_column("Sessions", justify="right")
    t.add_column("Bets",     justify="right")
    t.add_column("Win rate", justify="right")
    t.add_column("P/L",      justify="right")
    t.add_column("Wagered",  justify="right")
    for r in rows:
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "—"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        t.add_row(str(r[key_attr]), f"{r['sessions']:,}", f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}")
    console.print(t)


def _print_top_sessions_table(title: str, rows: list):
    if not rows:
        return
    t = Table(title=title, box=box.SIMPLE, expand=True)
    t.add_column("ID",       justify="right")
    t.add_column("Game",     style="cyan")
    t.add_column("Strategy", style="magenta")
    t.add_column("Bets",     justify="right")
    t.add_column("Win rate", justify="right")
    t.add_column("P/L",      justify="right")
    t.add_column("Wagered",  justify="right")
    t.add_column("Started",  style="dim")
    for r in rows:
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "—"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        started_short = (r['started_at'] or "").replace("T", " ")[:19]
        t.add_row(str(r['id']), r['game'], r['strategy'], f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}",
                  started_short)
    console.print(t)


def cmd_analytics(since: str = None, until: str = None, lane: int = None):
    """Analytics report over saved sessions, optionally windowed by started_at and lane."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found — run a session first.[/]")
        return
    init_db()  # sessions.lane on DBs the bot hasn't opened yet
    try:
        where, wparams, wlabel = stats_date_window(since, until, lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return
    lifetime = _analytics_lifetime(where, wparams)
    if lifetime["sessions_total"] == 0:
        console.print("[yellow]No sessions in the DB yet — run a session first.[/]")
        return

    console.print(Rule(
        f"[bold cyan]{'Range' if (since or until) else 'Lifetime'} analytics[/] — "
        f"{lifetime['sessions_total']:,} sessions, "
        f"{lifetime['bets_total']:,} bets, {lifetime['wagered_total']:.8f} wagered"
        f"{wlabel}"))

    p = lifetime['profit_total']
    pc = 'green' if p >= 0 else 'red'
    ps = '+' if p >= 0 else ''
    grid = Table.grid(padding=(0, 2))
    grid.add_row("Total sessions:",     f"{lifetime['sessions_total']:,}")
    conn = _db_conn()
    try:
        a_first, a_last, a_span, a_bet = sessions_time_span(conn, where, wparams)
    finally:
        conn.close()
    if a_first:
        grid.add_row("Time range:", f"{a_first.isoformat(' ', 'seconds')} → "
                                    f"{a_last.isoformat(' ', 'seconds')}")
    grid.add_row("Time span:",          fmt_duration(a_span))
    grid.add_row("Bet time:",           fmt_duration(a_bet))
    grid.add_row("Sessions in profit:", f"{lifetime['sessions_in_profit']:,}")
    grid.add_row("Sessions in loss:",   f"{lifetime['sessions_in_loss']:,}")
    grid.add_row("Total bets:", f"{lifetime['bets_total']:,}   W: {lifetime['wins_total']:,}   "
                                f"L: {lifetime['losses_total']:,}")
    grid.add_row("Win rate:",           f"{lifetime['win_rate_pct']:.1f}%")
    grid.add_row("Total wagered:",      f"{lifetime['wagered_total']:.8f}")
    grid.add_row("Total P/L:",          f"[{pc}]{ps}{p:.8f}[/]")
    grid.add_row("Best session:",       f"+{lifetime['best_profit']:.8f}")
    grid.add_row("Worst session:",      f"{lifetime['worst_profit']:.8f}")
    console.print(Panel(grid, title="[bold]Overall[/]", border_style=pc))

    _print_analytics_table("[bold]By game[/]", _analytics_by_game(where, wparams), "game", "Game")
    _print_analytics_table("[bold]By strategy[/]", _analytics_by_strategy(where, wparams),
                           "strategy", "Strategy")
    by_lane = _analytics_by_lane(where, wparams)
    if len(by_lane) > 1:
        _print_analytics_table("[bold]By lane[/]", by_lane, "lane", "Lane")

    _print_top_sessions_table("[bold]Top 5 best sessions[/]",
                              _analytics_top_sessions("profit_desc", 5, where, wparams))
    _print_top_sessions_table("[bold]Top 5 worst sessions[/]",
                              _analytics_top_sessions("profit_asc", 5, where, wparams))

    console.print("\n[dim]  Use --stats for recent sessions in detail, "
                  "or --session-bets ID to drill into one.[/]\n")


def cmd_status():
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running session found.[/]")
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception:
        console.print("[red]Could not read state file.[/]")
        return

    pid = d.get("pid", 0)
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass

    status = "[bold green]RUNNING[/]" if alive else "[bold red]STOPPED[/]"
    p = d.get("profit", 0)
    ps = "+" if p >= 0 else ""
    pc = "green" if p >= 0 else "red"
    bets = d.get("total_bets", 0)
    wins = d.get("wins", 0)
    losses = d.get("losses", 0)
    wr = (wins / bets * 100) if bets > 0 else 0
    uptime = str(timedelta(seconds=d.get("uptime_sec", 0)))
    hi_bal = d.get("highest_balance", 0)
    lo_bal = d.get("lowest_balance", 0)
    hi_win = d.get("highest_win", 0)
    big_loss = d.get("biggest_loss", 0)
    bpm = d.get("bets_per_minute", 0)
    bps = d.get("bets_per_second", 0)
    pk_bps = int(d.get("peak_bps", 0))
    lw_bps = int(d.get("low_bps", 0))
    pk_bpm = int(d.get("peak_bpm", 0))
    lw_bpm = int(d.get("low_bpm", 0))
    start_bal = d.get("start_balance", 0)
    cur_bal = d.get("current_balance", 0)
    avg_pnl = p / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    cs = d.get("current_streak", 0)
    if cs > 0:
        sk_now = f"W+{cs}"
    elif cs < 0:
        sk_now = f"L{cs}"
    else:
        sk_now = "—"

    game = d.get("game", "limbo")
    game_label = GAMES.get(game, {}).get("label", game)
    if game == "dice":
        game_info = f"{game_label} {d.get('dice_condition', 'above')} {d.get('dice_target', 50.5)}"
    else:
        game_info = game_label

    console.print(Rule(f"[bold cyan]Shuffle Session #{d.get('session_id', '?')}  {status}[/]"))

    mult = d.get("multiplier_target", 0)
    strat_str = f"{d.get('strategy', '?')}  {mult}x  {game_info}"
    sk = d.get("strategy_key", "")
    if sk in ("2", "6", "7"):
        strat_str += f"  [dim](loss mult: {d.get('loss_mult', '?')}x)[/]"
    elif sk in ("3", "5"):
        strat_str += f"  [dim](win mult: {d.get('win_mult', '?')}x)[/]"

    rows = [
        ("Uptime",         uptime),
        *([("Running for", fmt_duration(d["run_uptime_sec"]))]
          if d.get("run_uptime_sec") is not None else []),
        ("Game",           game_info),
        ("Currency",       d.get("currency", "?").upper()),
        ("Strategy",       strat_str),
        ("Base / Current", f"{d.get('base_bet', 0):.8f} / {d.get('current_bet', 0):.8f}"),
        ("Highest Bet",    f"{d.get('highest_bet', 0):.8f}"),
    ]

    delay = d.get("bet_delay", 0)
    if delay:
        rows.append(("Bet Delay",     f"{delay}s"))

    rows += [
        ("Balance",        f"{start_bal:.8f} → {cur_bal:.8f}"),
        ("Peak / Low",     f"[green]{hi_bal:.8f}[/] / [red]{lo_bal:.8f}[/]"),
        ("Profit",         f"[{pc}]{ps}{p:.8f}[/]"),
        ("Wagered",        f"{d.get('wagered', 0):.8f}"),
        ("Avg P/L",        f"[{avg_c}]{avg_s}{avg_pnl:.8f}[/] per bet"),
        ("Bets",           str(bets)),
        ("Wins / Losses",  f"[green]{wins}[/] / [red]{losses}[/]"),
        ("Win Rate",       f"{wr:.1f}%"),
        ("Current Streak", sk_now),
        ("Best Streaks",   f"W+{d.get('max_win_streak', 0)}  L-{d.get('max_loss_streak', 0)}"),
        ("Best Win",       f"[green]+{hi_win:.8f}[/]"),
        ("Worst Loss",     f"[red]-{big_loss:.8f}[/]"),
        ("Speed",          f"{bps:.1f}/s  {bpm:.0f}/m  Range: {lw_bps}-{pk_bps}/s  {lw_bpm}-{pk_bpm}/m"),
    ]
    # v1.9.0 — API / cycle / efficiency (WolfBet v2.17.0 / v2.30.3-v2.30.5)
    api_p50 = float(d.get("api_p50_ms") or 0)
    api_peak = float(d.get("api_peak_ms") or 0)
    if api_p50 or api_peak:
        rows.append(("API", f"p50 {api_p50:.0f}ms · p99 {float(d.get('api_p99_ms') or 0):.0f}ms"
                            f" · peak {api_peak:.0f}ms"))
    if d.get("cycle_ms"):
        rows.append(("Cycle", f"{float(d['cycle_ms']):.0f}ms/bet (overhead "
                              f"{float(d.get('overhead_ms') or 0):.0f}ms) → "
                              f"{float(d.get('theoretical_max_bps') or 0):.1f} BPS ceiling"))
    if d.get("efficiency_pct"):
        rows.append(("Efficiency", f"{float(d['efficiency_pct']):.0f}% of theoretical"))

    # Stop conditions
    stop_parts = []
    mp = d.get("max_profit")
    if mp is not None:
        stop_parts.append(f"Profit >= {mp}")
    ml = d.get("max_loss")
    if ml is not None:
        stop_parts.append(f"Loss >= {ml}")
    mb = d.get("max_bets")
    if mb is not None:
        stop_parts.append(f"Bets >= {mb}")
    mw = d.get("max_wins")
    if mw is not None:
        stop_parts.append(f"Wins >= {mw}")
    sb = d.get("stop_on_balance")
    if sb is not None:
        stop_parts.append(f"Balance <= {sb}")
    if stop_parts:
        rows.append(("Stop Conditions", " | ".join(stop_parts)))
    else:
        rows.append(("Stop Conditions", "[dim]None set[/]"))

    pi = d.get("profit_increment")
    pt = d.get("profit_threshold")
    if pi and pt:
        rows.append(("Profit Bump",   f"+{pi:.8f} every {pt} profit"))

    rows += [
        ("Last Status",    d.get("status", "")[:80]),
        ("Updated",        d.get("updated_at", "?").replace("T", " ")),
    ]

    for k, v in rows:
        console.print(f"  [dim]{k:18s}[/] {v}")
    if d.get("last_error"):
        console.print(f"  [dim]{'Last Error':18s}[/] [red]{d['last_error'][:60]}[/]")

    rc = d.get("rule_count", 0)
    if rc > 0:
        console.print()
        console.print(f"  [yellow]Rules ({rc}):[/]")
        for i, desc in enumerate(d.get("rule_descriptions", []), 1):
            console.print(f"    [dim]{i}.[/] {desc}")

    console.print()

SYSTEMD_UNIT = "shuffle"


def _systemd_unit() -> str:
    """This lane's unit: `shuffle` for lane 1, `shuffle-lane@K` for lanes 2..N."""
    return SYSTEMD_UNIT if state.lane <= 1 else f"{SYSTEMD_UNIT}-lane@{state.lane}"


def _is_systemd_managed(pid) -> bool:
    """True when pid runs inside this lane's shuffle systemd unit (cgroup check)."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            return f"{_systemd_unit()}.service" in f.read()
    except OSError:
        return False


def cmd_stop():
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running session found.[/]")
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        pid = d.get("pid", 0)
        if pid:
            os.kill(pid, signal.SIGTERM)
            console.print(f"[green]Sent SIGTERM to PID {pid}[/]")
        else:
            console.print("[yellow]No PID found in state file.[/]")
    except ProcessLookupError:
        console.print("[yellow]Process not running.[/]")
        _cleanup_state_file()
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Error{detail}.[/]")

def cmd_list_presets():
    presets = list_presets()
    if not presets:
        console.print("[dim]No saved presets.[/]")
        return
    for name, data in presets.items():
        game = data.get("game", "limbo")
        console.print(f"  [yellow]{name}[/]  [dim]->[/] {data.get('strategy', '?')} on "
                      f"{game}/{data.get('currency', '?').upper()} @ {data.get('base_bet', 0):.8f}  "
                      f"{data.get('multiplier_target', 0)}x")

def cmd_last_bets(n: int):
    """Show last N bets from database."""
    init_db()
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()
    rows = conn.execute("""
        SELECT b.id, b.session_id, substr(b.timestamp,1,19) AS dt,
               COALESCE(b.game, 'limbo') AS game,
               b.amount, b.result_value, COALESCE(b.result_display, '') AS rd,
               b.state, b.profit, b.balance_after
        FROM bets b ORDER BY b.id DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]No bets found.[/]")
        return

    console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Bet",      width=7,  justify="right")
    t.add_column("Sess",     width=5,  justify="center")
    t.add_column("DateTime", width=19)
    t.add_column("Game",     width=6)
    t.add_column("Amount",   width=14, justify="right")
    t.add_column("Result",   width=10, justify="right")
    t.add_column("W/L",      width=4,  justify="center")
    t.add_column("P/L",      width=15, justify="right")
    t.add_column("Balance",  width=15, justify="right")

    for r in rows:
        bid, sid, dt, game, amt, roll, rd, st, pnl, bal = r
        rc = "bold green" if st == "win" else "bold red"
        pcc = "green" if (pnl or 0) >= 0 else "red"
        pss = "+" if (pnl or 0) >= 0 else ""
        result_str = rd if rd else (f"{roll:.2f}" if roll else "?")
        game_label = GAMES.get(game, {}).get("label", game or "?")
        t.add_row(
            str(bid), str(sid), dt or "?",
            game_label,
            f"{amt:.8f}" if amt else "?",
            result_str,
            Text("W" if st == "win" else "L", style=rc),
            Text(f"{pss}{pnl:.8f}" if pnl is not None else "?", style=pcc),
            f"{bal:.8f}" if bal else "?"
        )
    console.print(t)
    console.print()


def _compute_streak_distribution(conn, session_id: int) -> dict:
    """Compute streak distribution from bets table."""
    rows = conn.execute(
        "SELECT state FROM bets WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()
    dist = {"win": {}, "loss": {}}
    if not rows:
        return dist
    cur_type = None
    cur_len = 0
    for (st,) in rows:
        kind = "win" if st == "win" else "loss"
        if kind == cur_type:
            cur_len += 1
        else:
            if cur_type and cur_len > 0:
                dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
            cur_type = kind
            cur_len = 1
    if cur_type and cur_len > 0:
        dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
    return dist


def cmd_session_bets(session_id: int):
    """Show full detailed stats and bets for a specific session."""
    init_db()
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()

    sess = conn.execute("""
        SELECT id, started_at, ended_at,
               UPPER(currency), COALESCE(game, 'limbo'), strategy, base_bet, multiplier,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0)
        FROM sessions WHERE id = ?
    """, (session_id,)).fetchone()

    if not sess:
        console.print(f"[red]Session #{session_id} not found.[/]")
        conn.close()
        return

    sid, started, ended, cur, game, strat, base, mult, bets, wins, losses, \
        profit, wagered, start_bal, end_bal, mws, mls, \
        hi_bal, lo_bal, hi_win, big_loss, \
        bpm, bps, s_pk_bps, s_lw_bps, s_pk_bpm, s_lw_bpm = sess

    bets = bets or 0; wins = wins or 0; losses = losses or 0
    profit = profit or 0; wagered = wagered or 0
    start_bal = start_bal or 0; end_bal = end_bal or 0
    mws = mws or 0; mls = mls or 0; mult = mult or 0; base = base or 0
    hi_bal = hi_bal or 0; lo_bal = lo_bal or 0
    hi_win = hi_win or 0; big_loss = big_loss or 0
    bpm = bpm or 0; bps = bps or 0
    s_pk_bps = int(s_pk_bps or 0); s_lw_bps = int(s_lw_bps or 0)
    s_pk_bpm = int(s_pk_bpm or 0); s_lw_bpm = int(s_lw_bpm or 0)

    pc = "green" if profit >= 0 else "red"
    ps = "+" if profit >= 0 else ""
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"
    avg_pnl = profit / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    game_label = GAMES.get(game, {}).get("label", game or "?")

    uptime_str = "—"
    try:
        t0 = datetime.fromisoformat(started) if started else None
        t1 = datetime.fromisoformat(ended) if ended else datetime.now()
        if t0:
            delta = int((t1 - t0).total_seconds())
            h, rem = divmod(max(delta, 0), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
    except Exception:
        pass

    console.print(Rule(f"[bold cyan]Session #{sid} — Full Stats[/]"))
    console.print(f"  [dim]Started:[/]       {(started or '?').replace('T', ' ')}")
    console.print(f"  [dim]Ended:[/]         {(ended or 'running').replace('T', ' ')}")
    console.print(f"  [dim]Uptime:[/]        {uptime_str}")
    console.print(f"  [dim]Game:[/]          {game_label}")
    console.print(f"  [dim]Currency:[/]      {cur}")
    console.print(f"  [dim]Strategy:[/]      {strat}  {mult}x  Base: {base:.8f}")
    console.print(f"  [dim]Bets:[/]          {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{profit:.8f}[/]")
    console.print(f"  [dim]Wagered:[/]       {wagered:.8f}")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_c}]{avg_s}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Balance:[/]       {start_bal:.8f} -> {end_bal:.8f}")
    console.print(f"  [dim]Peak / Low:[/]    [green]{hi_bal:.8f}[/] / [red]{lo_bal:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{mws}  L-{mls}")
    console.print(f"  [dim]Best Win:[/]      [green]+{hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{big_loss:.8f}[/]")
    console.print(f"  [dim]Speed:[/]         {bps:.1f} avg/s  {bpm:.0f} avg/m  Range: {s_lw_bps}-{s_pk_bps}/s  {s_lw_bpm}-{s_pk_bpm}/m")

    console.print()
    console.print(Rule("[bold cyan]Streak Distribution[/]"))

    dist = _compute_streak_distribution(conn, session_id)

    loss_dist = dist["loss"]
    if loss_dist:
        console.print(f"  [bold red]Loss Streaks[/]  (how many times each streak length occurred)")
        t_loss = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        t_loss.add_column("Streak", style="bold red", justify="right")
        t_loss.add_column("Count", justify="right")
        t_loss.add_column("", width=30)
        max_count = max(loss_dist.values()) if loss_dist else 1
        for length in sorted(loss_dist.keys()):
            count = loss_dist[length]
            bar_w = int(count / max_count * 25)
            bar = "[red]" + "\u2588" * bar_w + "[/]"
            t_loss.add_row(f"L-{length}", str(count), bar)
        console.print(t_loss)
    else:
        console.print("  [dim]No loss streaks recorded[/]")

    console.print()

    win_dist = dist["win"]
    if win_dist:
        console.print(f"  [bold green]Win Streaks[/]  (how many times each streak length occurred)")
        t_win = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        t_win.add_column("Streak", style="bold green", justify="right")
        t_win.add_column("Count", justify="right")
        t_win.add_column("", width=30)
        max_count = max(win_dist.values()) if win_dist else 1
        for length in sorted(win_dist.keys()):
            count = win_dist[length]
            bar_w = int(count / max_count * 25)
            bar = "[green]" + "\u2588" * bar_w + "[/]"
            t_win.add_row(f"W+{length}", str(count), bar)
        console.print(t_win)
    else:
        console.print("  [dim]No win streaks recorded[/]")

    rows = conn.execute("""
        SELECT id, substr(timestamp,12,8), COALESCE(game, 'limbo'),
               amount, result_value, COALESCE(result_display, ''),
               state, profit, balance_after
        FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 50
    """, (session_id,)).fetchall()
    conn.close()

    if rows:
        console.print()
        console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
        t = Table(box=box.SIMPLE, expand=True)
        t.add_column("Bet",     width=7, justify="right")
        t.add_column("Time",    width=9)
        t.add_column("Game",    width=6)
        t.add_column("Amount",  width=14, justify="right")
        t.add_column("Result",  width=10, justify="right")
        t.add_column("W/L",     width=4, justify="center")
        t.add_column("P/L",     width=15, justify="right")
        t.add_column("Balance", width=15, justify="right")

        for r in rows:
            bid, tm, g, amt, roll, rd, st, pnl, bal = r
            rc = "bold green" if st == "win" else "bold red"
            pcc = "green" if (pnl or 0) >= 0 else "red"
            pss = "+" if (pnl or 0) >= 0 else ""
            result_str = rd if rd else (f"{roll:.2f}" if roll else "?")
            gl = GAMES.get(g, {}).get("label", g or "?")
            t.add_row(
                str(bid), tm or "?",
                gl,
                f"{amt:.8f}" if amt else "?",
                result_str,
                Text("W" if st == "win" else "L", style=rc),
                Text(f"{pss}{pnl:.8f}" if pnl is not None else "?", style=pcc),
                f"{bal:.8f}" if bal else "?"
            )
        console.print(t)
    console.print()


# ── cmd_monitor ───────────────────────────────────────────
def _monitor_read_state():
    """Read daemon state file and return dict, or None if not running."""
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None

def _monitor_get_pid(d: dict) -> int:
    """Get daemon PID and verify it's alive."""
    pid = d.get("pid", 0)
    if pid:
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            pass
    return 0

def _monitor_get_recent_bets(session_id: int, limit: int) -> list:
    """Fetch recent bets from DB for the monitor view."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = _db_conn()
        rows = conn.execute("""
            SELECT id, substr(timestamp,1,19), COALESCE(game, 'limbo'),
                   amount, result_value, COALESCE(result_display, ''),
                   state, profit, balance_after
            FROM bets WHERE session_id=? ORDER BY id DESC LIMIT ?
        """, (session_id, limit)).fetchall()
        conn.close()
        result = []
        for r in rows:
            result.append({
                "n": r[0], "time": (r[1] or "")[11:19],
                "game": r[2] or "limbo",
                "amt": r[3] or 0, "roll": r[4] or 0,
                "rd": r[5] or "",
                "state": r[6] or "lose", "pnl": r[7] or 0,
                "bal": r[8] or 0,
            })
        return result
    except Exception:
        return []

def _build_monitor_screen(d: dict, bets: list, pid_alive: bool) -> str:
    """Build full-screen ANSI dashboard from state dict + bet list."""
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h, term_w = 24, 80

    lines = []
    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")
    def add_blank():
        lines.append(_CLR_EOL)

    sid       = d.get("session_id", "?")
    uptime    = str(timedelta(seconds=d.get("uptime_sec", 0)))
    currency  = d.get("currency", "?").upper()
    strategy  = d.get("strategy", "?")
    mult      = d.get("multiplier_target", 0)
    game      = d.get("game", "limbo")
    base_bet  = d.get("base_bet", 0)
    cur_bet   = d.get("current_bet", 0)
    hi_bet    = d.get("highest_bet", 0)
    bal       = d.get("current_balance", 0)
    start_bal = d.get("start_balance", 0)
    profit    = d.get("profit", 0)
    wagered   = d.get("wagered", 0)
    total     = d.get("total_bets", 0)
    wins      = d.get("wins", 0)
    losses    = d.get("losses", 0)
    cs        = d.get("current_streak", 0)
    mws       = d.get("max_win_streak", 0)
    mls       = d.get("max_loss_streak", 0)
    hi_bal    = d.get("highest_balance", 0) or bal
    lo_bal    = d.get("lowest_balance", 0) or bal
    hi_win    = d.get("highest_win", 0)
    big_loss  = d.get("biggest_loss", 0)
    bps       = d.get("bets_per_second", 0)
    bpm       = d.get("bets_per_minute", 0)
    pk_bps    = int(d.get("peak_bps", 0))
    lw_bps    = int(d.get("low_bps", 0))
    pk_bpm    = int(d.get("peak_bpm", 0))
    lw_bpm    = int(d.get("low_bpm", 0))
    paused    = d.get("paused", False)
    status    = d.get("status", "")

    game_label = GAMES.get(game, {}).get("label", game)
    if game == "dice":
        game_info = f"Dice {d.get('dice_condition', 'above')} {d.get('dice_target', 50.5)}  {mult}x"
    else:
        game_info = f"Limbo {mult}x target"

    _pause_left = int(d.get("pause_remaining_sec", 0) or 0)
    _pause_total = int(d.get("pause_total_sec") or d.get("recurring_delay_sec", 0) or 0)
    if pid_alive and _pause_left > 0:
        st_ind = _a("bold_cyan", f"◐ PAUSED {timedelta(seconds=_pause_left)}/"
                                 f"{timedelta(seconds=_pause_total)}")
    elif pid_alive and not paused:
        st_ind = _a("bold_green", "* LIVE")
    elif pid_alive and paused:
        st_ind = _a("bold_yellow", "* PAUSED")
    else:
        st_ind = _a("bold_red", "* STOPPED")

    _run = d.get("run_uptime_sec")
    run_seg = f"  Run {fmt_duration(_run)}" if _run is not None else ""
    add(f"{_a('dim_magenta', '--- ')}{_a('bold_cyan', f'Shuffle v{VERSION}')}"
        f"{_a('white', f'  #{sid}  {uptime}{run_seg}')}  {st_ind} "
        f"{_a('dim', f'  {game_info}')}"
        f"{_a('dim_magenta', ' ' + '-' * max(0, term_w - 80))}")

    ps = "+" if profit >= 0 else ""
    pc = "bold_green" if profit >= 0 else "bold_red"
    wr = (wins / total * 100) if total else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"
    add(f"{_COLORS['bg_magenta']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{bal:.8f} {currency}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{profit:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "--")
    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(total))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{mws}/L-{mls}')}")

    s4 = f" {_a('dim', 'Bet')} {_a('yellow', f'{cur_bet:.8f}')}"
    s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{hi_bet:.8f}')}"
           f"  {_a('cyan', strategy)}"
           f"  {_a('dim', f'{mult}x')}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{bps:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{bpm:.0f}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', f'{lw_bps}-{pk_bps}/s {lw_bpm}-{pk_bpm}/m')}")
    add(s4)

    avg_pnl = profit / total if total else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = bal - start_bal
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""
    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{hi_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{big_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    add(_build_config_progress_row(d))

    add(_a("dim_magenta", "-" * term_w))

    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Game':<6} {'Amount':>14}  {'Result':>10}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    bets_avail = max(0, term_h - 10)
    shown = bets[:bets_avail]

    for b in shown:
        bn = b["n"]
        btm = b["time"]
        bgame = GAMES.get(b["game"], {}).get("label", b["game"])[:6]
        bamt = b["amt"]
        broll = b["roll"]
        brd = b["rd"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if bpnl >= 0 else "red"
        bsign = "+" if bpnl >= 0 else ""
        result_str = brd if brd else f"{broll:>10.2f}"
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('dim', f'{bgame:<6}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{result_str:>10}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    for _ in range(bets_avail - len(shown)):
        add_blank()

    add(_a("dim_magenta", "-" * term_w))

    status_txt = status[:60] if status else "Monitoring..."
    keys = "[P]ause [R]esume [Q]uit/Detach [S]top daemon"
    add(f" {_a('bold_white', 'MONITOR')} {_a('white', status_txt)}  {_a('dim', keys)}")
    # v1.9.0 — T / H are foreground-TUI only; advertise the config path (WolfBet v2.28.1).
    add(f" {_a('dim', 'Tweak config:')} {_a('cyan', 'shuffle.py --config-set KEY=VALUE')}   "
        f"{_a('dim', '· History:')} {_a('cyan', 'shufflectl stats / analytics')}")

    return "\r\n".join(lines)


def cmd_monitor():
    """Attach a live TUI viewer to a running daemon session."""
    d = _monitor_read_state()
    if not d:
        console.print("[yellow]No running session found. Start with: shufflectl start[/]")
        return
    pid = _monitor_get_pid(d)
    if not pid:
        console.print("[yellow]Daemon not running (stale state file).[/]")
        return

    sid = d.get("session_id", 0)
    console.print(f"[dim]Attaching to session #{sid} (PID {pid})... Press Q to detach.[/]")
    time.sleep(0.5)

    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    _monitor_running = [True]
    _monitor_action = [None]

    def _monitor_keys():
        while _monitor_running[0]:
            try:
                import termios as _t, tty as _tty
                fd = sys.stdin.fileno()
                old = _t.tcgetattr(fd)
                try:
                    _tty.setraw(fd)
                    ch = sys.stdin.read(1).lower()
                finally:
                    _t.tcsetattr(fd, _t.TCSADRAIN, old)
                if ch in ("q", "\x03", "\x1c"):
                    _monitor_action[0] = "quit"
                    _monitor_running[0] = False
                elif ch == "p":
                    _monitor_action[0] = "pause"
                elif ch == "r":
                    _monitor_action[0] = "resume"
                elif ch == "s":
                    _monitor_action[0] = "stop"
                    _monitor_running[0] = False
            except Exception:
                time.sleep(0.1)

    t_keys = threading.Thread(target=_monitor_keys, daemon=True)
    t_keys.start()

    try:
        while _monitor_running[0]:
            action = _monitor_action[0]
            _monitor_action[0] = None
            if action == "pause" and pid:
                try:
                    os.kill(pid, signal.SIGUSR1)
                except OSError:
                    pass
            elif action == "resume" and pid:
                try:
                    os.kill(pid, signal.SIGUSR2)
                except OSError:
                    pass

            d = _monitor_read_state()
            if not d:
                break
            pid = _monitor_get_pid(d)

            bet_limit = max(0, (os.get_terminal_size().lines if sys.stdout.isatty() else 24) - 11)
            recent_bets = _monitor_get_recent_bets(sid, bet_limit)

            sys.stdout.write("\033[H")
            sys.stdout.write(_build_monitor_screen(d, recent_bets, bool(pid)))
            sys.stdout.flush()
            time.sleep(0.5)

        if _monitor_action[0] == "stop" or (locals().get("action") == "stop"):
            # v1.9.0 — under systemd, stop the unit so Restart= can't respawn it
            # (WolfBet v2.28.1); otherwise SIGTERM.
            stopped_via_systemd = False
            if _is_systemd_managed(pid):
                try:
                    import subprocess as _sp
                    _sp.run(["systemctl", "--user", "stop", _systemd_unit()],
                            check=False, timeout=10)
                    stopped_via_systemd = True
                except Exception:
                    pass
            if not stopped_via_systemd and pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    finally:
        _monitor_running[0] = False
        sys.stdout.write("\033[?25h\033[?1049l\033[0m")
        sys.stdout.flush()
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass

    if _monitor_action[0] == "stop":
        console.print("[bold red]Daemon stopped.[/]")
    else:
        console.print("[dim]Detached from monitor. Daemon continues running.[/]")


# ===========================================================
#  MAIN
# ===========================================================
def _load_and_connect():
    if not load_config():
        console.print("[red]No saved config found. Run without --resume first.[/]")
        sys.exit(1)
    console.print("[dim]Loading saved config...[/]")
    game_label = GAMES.get(state.game, {}).get("label", state.game)
    try:
        api_test_connection()
        balances = api_get_balances()
        for b in balances:
            if b["currency"].lower() == state.currency.lower():
                bal = b["amount"]
                state.start_balance = bal
                state.current_balance = bal
                state.highest_balance = bal
                state.lowest_balance = bal
                break
        console.print(f"[green]Resumed -- {game_label} / {state.strategy} on {state.currency.upper()} "
                      f"@ {state.base_bet:.8f} base bet[/]")
        if state.start_balance > 0:
            console.print(f"  [dim]Balance:[/] {state.start_balance:.8f} {state.currency.upper()}")
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
        sys.exit(1)

# ===========================================================
#  CONFIG CLI  (v1.9.0 — WolfBet v2.25.0)
# ===========================================================
_CONFIG_SHOW_FIELDS = [
    ("currency",              "Betting currency"),
    ("game",                  "Game (limbo / dice)"),
    ("multiplier_target",     "Payout multiplier"),
    ("dice_target",           "Dice target (derived from multiplier)"),
    ("dice_condition",        "Dice direction (above / below)"),
    ("strategy",              "Strategy name"),
    ("strategy_key",          "Strategy key (1-7)"),
    ("base_bet",              "Base bet amount"),
    ("loss_mult",             "Loss multiplier (Martingale)"),
    ("win_mult",              "Win multiplier (Paroli / Anti-M)"),
    ("delay_martin_threshold","Delay Martingale N-loss threshold"),
    ("bet_delay",             "Seconds between bets (0 = full speed)"),
    ("max_profit",            "Stop when profit ≥ this"),
    ("max_loss",              "Stop when loss ≥ this"),
    ("max_bets",              "Stop after this many bets"),
    ("max_wins",              "Stop after this many wins"),
    ("stop_on_balance",       "Stop when balance floor reached"),
    ("profit_increment",      "Bump base_bet by this at every profit milestone"),
    ("profit_threshold",      "Profit interval between increments"),
    ("recurring",             "Auto-restart cycle on non-loss stop"),
    ("recurring_delay",       "Seconds to pause between recurring cycles (0 = back-to-back)"),
    ("streak_reset_losses",   "Restart session on first win after N losses in a row (profit ≥ 0)"),
    ("streak_reset_delay",    "Seconds to pause before the streak-reset restart"),
    ("basebet_pct",           "Base bet as % of balance (0 = off)"),
    ("streak_delay_loss",     "Extra pause after N losses in a row  (N:sec)"),
    ("streak_delay_win",      "Extra pause after N wins in a row  (N:sec)"),
    ("streak_delay_bets",     "Extra pause every N bets  (N:sec)"),
    ("streakbet_loss",        "Bet reduction after N losses  (N:factor)"),
    ("milestone_bets",        "Milestone every N bets (0 = off)"),
    ("milestone_wins",        "Milestone every N wins (0 = off)"),
    ("milestone_losses",      "Milestone every N losses (0 = off)"),
    ("milestone_profit",      "Milestone every X profit crossed (0 = off)"),
    ("proxy",                 "HTTP/HTTPS/SOCKS5 proxy URL (credentials masked)"),
    ("purge_days",            "Bet-row retention window in days (1-30)"),
    ("db_write_mode",         "Bet DB writer: async (default, batched) or sync (per-bet commit)"),
    ("custom_rules_text",     "Rule-Based custom rules"),
]
_MONEY_FIELDS = ("base_bet", "max_profit", "max_loss", "stop_on_balance",
                 "profit_increment", "profit_threshold", "milestone_profit")


def _fmt_config_value(field: str, val) -> str:
    """Render a config value for --config-show. Proxy is always credential-masked."""
    if field == "proxy":
        return f"[cyan]{mask_proxy(val)}[/]" if val else "[dim]direct[/]"
    if val is None:
        return "[dim]off[/]"
    if isinstance(val, bool):
        return "[green]on[/]" if val else "[dim]off[/]"
    if isinstance(val, (tuple, list)) and len(val) == 2:
        n, x = val
        return "[dim]off[/]" if not n else f"{n}:{x:g}"
    if isinstance(val, float):
        if field in _MONEY_FIELDS:
            return f"{val:.8f}"
        return f"{val:.4f}".rstrip("0").rstrip(".") or "0"
    if isinstance(val, str) and len(val) > 60:
        return val[:57] + "…"
    return str(val)


def cmd_config_show():
    """Print the saved config as a table. No wizard, no side effects."""
    if not load_config():
        console.print("[yellow]No config saved yet — run the wizard first.[/]")
        console.print("[dim]  Try: python3 shuffle.py --setup-only[/]")
        sys.exit(1)
    tbl = Table(title="Shuffle AutoBot — Current Config", title_style="bold cyan")
    tbl.add_column("Field",       style="bold yellow", no_wrap=True)
    tbl.add_column("Value",       style="cyan")
    tbl.add_column("Description", style="dim")
    tok_state = "[bold green](SET)[/]" if state.bearer_token else "[bold red](NOT SET)[/]"
    tbl.add_row("bearer_token", f"[dim]{_mask_key(state.bearer_token)}[/] {tok_state}",
                "Shuffle Bearer token (set via the wizard)")
    for field, desc in _CONFIG_SHOW_FIELDS:
        tbl.add_row(field, _fmt_config_value(field, getattr(state, field, None)), desc)
    console.print(tbl)
    _print_lanes_config()
    try:
        st = os.stat(CONFIG_PATH)
        console.print(f"\n[dim]Config file:[/] [cyan]{CONFIG_PATH}[/]  "
                      f"[dim]mode={oct(st.st_mode & 0o777)}  size={st.st_size}B[/]")
    except OSError:
        pass
    console.print("[dim]Tweak with:[/] [cyan]python3 shuffle.py --config-set KEY=VALUE …[/]")


def _read_config_raw() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _print_lanes_config() -> None:
    """--config-show: lane count, split mode and per-lane overrides (WolfBet v2.35.0 / v2.36.0)."""
    raw = _read_config_raw()
    n, mode = lanes_count(raw), lane_split(raw)
    line = f"\n[bold]Lanes:[/] [cyan]{n}[/]  [bold]split:[/] [cyan]{mode}[/]"
    if n > 1:
        line += f"  [dim]({LANE_SPLIT_HELP[mode]})[/]"
    console.print(line)
    overrides = (raw.get("lanes") or {}).get("overrides") or {}
    for k in range(2, LANES_MAX + 1):
        ov = overrides.get(str(k)) if isinstance(overrides, dict) else None
        if isinstance(ov, dict) and ov:
            pairs = ", ".join(f"{key}={val}" for key, val in ov.items())
            console.print(f"  [yellow]lane{k} overrides:[/] {pairs}")


def _signal_lane_daemons(lanes=None) -> int:
    """SIGHUP running lane daemons (default: lanes 2..N) via their pid files; return the count."""
    if not hasattr(signal, "SIGHUP"):
        return 0
    sent = 0
    for k in (lanes if lanes is not None else range(2, LANES_MAX + 1)):
        try:
            with open(lane_path(_BASE_PID_PATH, k)) as f:
                os.kill(int(f.read().strip()), signal.SIGHUP)
            sent += 1
        except (OSError, ValueError):
            continue
    return sent


def _lane_override_changes(pairs: list):
    """Lane pairs → ({key: stored value}, [keys to remove]). Raises ValueError.

    Values go through the same `_apply_mutation` checks as base keys, against the
    base config; derived fields (e.g. dice_target) come along. A key set
    explicitly is kept even when it equals the base value.
    """
    removals, rest = [], []
    for pair in pairs:
        key, val = pair.split("=", 1)
        key = key.strip().lower()
        canonical = _TWEAK_ALIAS.get(key, key)
        check_lane_keys([key, canonical], _LANE_FORBIDDEN_KEYS)
        if val.strip().lower() in LANE_UNSET_VALUES:
            removals.append(canonical)
        else:
            rest.append(f"{canonical}={val}")
    parsed = _parse_config_set_pairs(rest) if rest else {}
    check_lane_keys(parsed, _LANE_FORBIDDEN_KEYS)
    if not parsed:
        return {}, removals
    lane = state.lane
    state.lane = 1                      # validate against the base config
    try:
        if not load_config():
            raise ValueError("no config saved yet — run the wizard first")
        before = _serialize_keys(_CONFIG_KEYS)
        _apply_mutation(parsed)
        after = _serialize_keys(_CONFIG_KEYS)
    finally:
        state.lane = lane
    changes = {k: after[k] for k in after
               if (after[k] != before[k] or k in parsed) and k not in _LANE_FORBIDDEN_KEYS}
    return changes, removals


def _cmd_config_set_lanes(per_lane: dict, count, split) -> None:
    """Write `lanes.count`, `lanes.split` and per-lane overrides; SIGHUP the edited lanes."""
    raw = _read_config_raw()
    if not raw:
        console.print("[yellow]No config saved yet — run the wizard first.[/]")
        sys.exit(1)
    try:
        edits = {k: _lane_override_changes(v) for k, v in sorted(per_lane.items())}
    except ValueError as e:
        console.print(f"[red]Invalid lane setting:[/] {e}")
        sys.exit(2)
    block = raw.setdefault("lanes", {})
    if not isinstance(block, dict):
        block = raw["lanes"] = {}
    overrides = block.setdefault("overrides", {})
    console.print(Rule("[bold cyan]Lanes updated[/]"))
    if count is not None:
        console.print(f"  [yellow]lanes[/]: [dim]{lanes_count(raw)}[/]  →  [green]{count}[/]")
        block["count"] = count
    if split is not None:
        console.print(f"  [yellow]lane_split[/]: [dim]{lane_split(raw)}[/]  →  [green]{split}[/]"
                      f"  [dim]({LANE_SPLIT_HELP[split]})[/]")
        block["split"] = split
    for k, (changes, removals) in edits.items():
        ov = overrides.setdefault(str(k), {})
        for key in removals:
            if ov.pop(key, None) is not None:
                console.print(f"  [yellow]lane{k}.{key}[/]: override removed (uses base)")
        for key, val in changes.items():
            ov[key] = val
            console.print(f"  [yellow]lane{k}.{key}[/]: [green]{val}[/]")
        if not ov:
            overrides.pop(str(k), None)
    _write_config_raw(raw)
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")
    if edits:
        n = _signal_lane_daemons(list(edits))
        if n:
            console.print(f"[dim]Reloaded {n} running lane(s).[/]")
    if count is not None:
        console.print(f"[yellow]Note:[/] start / stop lanes to match: run each lane with "
                      f"[cyan]python3 shuffle.py --daemon --resume --lane K[/] (shufflectl lanes is E7).")
    elif split is not None:
        console.print("[yellow]Note:[/] the split applies on the next start of each lane. "
                      "Run [cyan]shufflectl restart[/] when ready.")


def _parse_config_set_pairs(pairs: list) -> dict:
    """['maxprofit=0.5', …] → {'max_profit': '0.5', …}; aliases applied. Raises ValueError."""
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Malformed pair (expected KEY=VALUE): {pair!r}")
        key, val = pair.split("=", 1)
        key = key.strip().lower()
        canonical = _TWEAK_ALIAS.get(key, key)
        if canonical not in _TWEAK_PARAMS:
            raise ValueError(f"Unknown config field: {key!r}. Valid: {', '.join(sorted(_TWEAK_PARAMS))}")
        out[canonical] = val.strip()
    return out


def cmd_config_set(pairs: list):
    """Apply KEY=VALUE mutations to the saved config, save, and SIGHUP a running daemon."""
    for p in pairs:
        if p.strip().lower().startswith(("bearer_token=", "token=")):
            console.print("[red]Refusing to write bearer_token via --config-set.[/]")
            console.print("[dim]  Set it with the wizard (--setup-only) so it never lands "
                          "in shell history / process args.[/]")
            sys.exit(2)
    # v1.10.0 — lanes=N, lane_split=MODE and laneK.key=value (WolfBet v2.35.0 / v2.36.0).
    try:
        pairs, per_lane, count, split = split_lane_pairs(pairs)
    except ValueError as e:
        console.print(f"[red]Invalid --config-set argument:[/] {e}")
        sys.exit(2)
    if per_lane or count is not None or split is not None:
        _cmd_config_set_lanes(per_lane, count, split)
        if not pairs:
            return
    try:
        parsed = _parse_config_set_pairs(pairs)
    except ValueError as e:
        console.print(f"[red]Invalid --config-set argument:[/] {e}")
        sys.exit(2)
    if not load_config():
        console.print("[yellow]No config saved yet — run the wizard first.[/]")
        console.print("[dim]  Try: python3 shuffle.py --setup-only[/]")
        sys.exit(1)
    old = {k: getattr(state, k, None) for k in parsed}
    try:
        msgs = _apply_mutation(parsed) or []
    except ValueError as e:
        console.print(f"[red]Mutation failed:[/] {e}")
        sys.exit(2)
    save_config()
    console.print(Rule("[bold cyan]Config updated[/]"))
    for k in parsed:
        console.print(f"  [yellow]{k}[/]: [dim]{_fmt_config_value(k, old[k])}[/]"
                      f"  →  [green]{_fmt_config_value(k, getattr(state, k, None))}[/]")
    for m in msgs:
        if m.startswith("⚠"):
            console.print(f"  [yellow]{m}[/]")
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")

    unsafe = sorted(k for k in parsed
                    if k not in _LIVE_RELOAD_KEYS or k == "db_write_mode")
    if unsafe:
        console.print(f"[yellow]Note:[/] {len(unsafe)} field(s) need a full restart to apply — "
                      f"{', '.join(unsafe)}. Run [cyan]shufflectl restart[/] when ready.")
    try:
        if not os.path.exists(PID_PATH):
            console.print("[dim]Daemon not running; changes apply on next start.[/]")
            return
        with open(PID_PATH) as f:
            daemon_pid = int(f.read().strip())
        if not hasattr(signal, "SIGHUP"):
            console.print("[dim]SIGHUP not supported on this platform; restart the daemon to apply.[/]")
            return
        os.kill(daemon_pid, signal.SIGHUP)
        console.print("[dim]Daemon reloaded live config.[/]")
    except (OSError, ValueError):
        console.print("[dim]Daemon PID stale; changes apply on next start.[/]")
    finally:
        # v1.10.0 — lanes 2..N read the same base config.
        n = _signal_lane_daemons()
        if n:
            console.print(f"[dim]Reloaded {n} running lane(s).[/]")


# ===========================================================
#  RETENTION  (v1.9.0 — WolfBet v2.26.0)
# ===========================================================
def cmd_cleanup(days=None):
    """Purge bet rows of sessions ended more than N days ago; session rows are kept."""
    load_config()
    n = int(days) if days is not None else int(state.purge_days)
    n = max(1, min(30, n))
    try:
        deleted = int(cleanup_old_bets(DB_PATH, bet_age_days=n) or 0)
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Cleanup failed{detail}.[/]")
        sys.exit(1)
    if deleted:
        console.print(f"[green]✓[/] Purged [bold]{deleted}[/] bet rows from sessions older than "
                      f"[bold]{n}[/] days. Session aggregates preserved.")
    else:
        console.print(f"[dim]Nothing to purge ({n}-day window; no old session bets found).[/]")


def _startup_purge() -> None:
    try:
        deleted = cleanup_old_bets(DB_PATH, bet_age_days=state.purge_days)
        if deleted:
            logger.info("STARTUP purged %d bet rows (>%dd)", deleted, state.purge_days)
    except Exception as e:
        logger.warning("startup purge failed: %s", e)


def _retention_worker():
    """Hourly purge of bet rows older than purge_days (mirrors the TG engine)."""
    while state.running or state.paused:
        for _ in range(3600):
            if not (state.running or state.paused):
                return
            time.sleep(1)
        try:
            deleted = cleanup_live_bets(DB_PATH, bet_age_days=state.purge_days)
            if deleted:
                logger.info("RETENTION purged %d bet rows (>%dd)", deleted, state.purge_days)
        except Exception as e:
            logger.warning("retention purge failed: %s", e)


# ===========================================================
#  BENCHMARK / DIAGNOSE  (v1.9.0 — WolfBet v2.26.1)
# ===========================================================
def _bench_lanes(n: int, lanes: int) -> dict:
    """`lanes` threads, each with its own client (as each lane process has), firing
    n read-only GetMyProfile queries at once. Returns rps / p50 / ok / total / s429."""
    payload = _gql_payload(_PROFILE_QUERY, {}, "GetMyProfile")
    lock = threading.Lock()
    samples, agg = [], {"ok": 0, "s429": 0}

    def worker():
        sess = _create_http()
        try:
            for _ in range(n):
                t0 = time.time()
                try:
                    r = sess.post(API_URL, headers=_headers(), json=payload, timeout=15)
                    ms = (time.time() - t0) * 1000
                    with lock:
                        samples.append(ms)
                        if r.status_code == 200:
                            agg["ok"] += 1
                        elif r.status_code == 429:
                            agg["s429"] += 1
                except Exception:
                    pass
        finally:
            try:
                sess.close()
            except Exception:
                pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(lanes)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = max(time.time() - t0, 1e-6)
    samples.sort()
    return {"rps": len(samples) / wall, "p50": percentile(samples, 0.50) if samples else 0.0,
            "ok": agg["ok"], "total": n * lanes, "s429": agg["s429"]}


def cmd_benchmark_lanes(n: int, lanes: int) -> None:
    """--benchmark --lanes M (WolfBet v2.35.0): throughput at 1..M concurrent clients."""
    lanes = max(2, min(LANES_MAX, int(lanes)))
    console.print(f"[dim]Benchmarking 1..{lanes} concurrent clients × {n} GetMyProfile "
                  f"(read-only) (proxy={mask_proxy(state.proxy)}) …[/]")
    rows = {k: _bench_lanes(n, k) for k in range(1, lanes + 1)}
    base = rows[1]["rps"]
    tbl = Table(title="Concurrent clients", title_style="bold cyan")
    for col in ("lanes", "req/s", "×1 lane", "p50 ms", "ok", "429"):
        tbl.add_column(col, justify="right")
    for k, r in rows.items():
        tbl.add_row(str(k), f"{r['rps']:.1f}", f"{(r['rps'] / base if base else 0):.2f}",
                    f"{r['p50']:.0f}", f"{r['ok']}/{r['total']}",
                    f"[red]{r['s429']}[/]" if r["s429"] else "0")
    console.print(tbl)
    rec = recommend_lanes(base, {k: v for k, v in rows.items() if k > 1})
    console.print(f"\n[bold]Recommended lanes:[/] [bold magenta]{rec}[/]  "
                  f"[dim](each added lane must gain ≥ 30% throughput, no 429s)[/]")
    console.print("[dim]The probe is the read-only GetMyProfile query; the bet mutations "
                  "may be rate limited differently. Set it with "
                  "[cyan]python3 shuffle.py --config-set lanes=N[/].[/]")


def cmd_benchmark(n=None, lanes=None):
    """Fire N read-only GetMyProfile queries and report the latency spread.

    Same `_http` session (proxy, TLS impersonation) as betting. No bet, no DB write.
    With lanes > 1, also measure 1..lanes concurrent clients.
    """
    n = 20 if n is None else int(n)
    n = max(3, min(200, n))
    if not load_config():
        console.print("[red]No config saved yet — run the wizard first.[/]")
        sys.exit(1)
    if lanes is not None and int(lanes) > 1:
        return cmd_benchmark_lanes(n, lanes)
    console.print(f"[dim]Benchmarking {n} × GetMyProfile (read-only) "
                  f"(proxy={mask_proxy(state.proxy)}) …[/]")
    payload = _gql_payload(_PROFILE_QUERY, {}, "GetMyProfile")
    samples, statuses, failures = [], [], 0
    for _ in range(n):
        t0 = time.time()
        try:
            r = _http.post(API_URL, headers=_headers(), json=payload, timeout=15)
            samples.append((time.time() - t0) * 1000)
            statuses.append(r.status_code)
        except Exception:
            failures += 1
            statuses.append("ERR")
    if not samples:
        console.print("[red]All requests failed. Check the token and network.[/]")
        sys.exit(1)
    samples.sort()
    p50 = percentile(samples, 0.50)
    p99 = percentile(samples, 0.99)
    mean = sum(samples) / len(samples)
    tbl = Table(title="API latency (ms)", title_style="bold cyan")
    for col, style in [("min", "green"), ("p50", "cyan"), ("mean", "cyan"),
                       ("p99", "yellow"), ("max", "red"), ("ok", "dim")]:
        tbl.add_column(col, style=style, justify="right")
    ok_count = sum(1 for st in statuses if st == 200)
    tbl.add_row(f"{min(samples):.0f}", f"{p50:.0f}", f"{mean:.0f}",
                f"{p99:.0f}", f"{max(samples):.0f}", f"{ok_count}/{n}")
    console.print(tbl)
    max_bps = 1000.0 / p50 if p50 > 0 else 0.0
    console.print(f"\n[dim]Theoretical max BPS from p50:[/] [bold magenta]{max_bps:.1f}[/] bps")
    colour, msg = benchmark_verdict(p50=p50, p99=p99, statuses=statuses,
                                    sample_count=n, casino_name="Shuffle")
    console.print(f"[bold {colour}]{msg}[/]")
    if failures:
        console.print(f"[dim]Note: {failures}/{n} network errors.[/]")


def cmd_diagnose():
    """One-shot bottleneck analysis of the running session, from the state file only."""
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running daemon session found.[/]\n"
                      "  Start one with [cyan]shufflectl start[/] then re-run "
                      "[cyan]python3 shuffle.py --diagnose[/].")
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Failed to read state file{detail}.[/]")
        return

    api_p50    = float(d.get("api_p50_ms") or 0)
    overhead   = float(d.get("overhead_ms") or 0)
    cycle_ms   = float(d.get("cycle_ms") or 0)
    cycle_p50  = float(d.get("cycle_p50_ms") or cycle_ms)
    cycle_p99  = float(d.get("cycle_p99_ms") or 0)
    cycle_n    = int(d.get("cycle_sample_count") or 0)
    recent_bps = float(d.get("bets_per_second_recent") or 0)
    bet_delay  = float(d.get("bet_delay") or 0)
    bps        = float(d.get("bets_per_second") or 0)
    tmax       = float(d.get("theoretical_max_bps") or 0)
    delay_ms   = bet_delay * 1000

    eff_ref = recent_bps if recent_bps > 0 else bps
    if tmax > 0:
        eff_raw = eff_ref / tmax * 100.0
        eff_display = ">100%" if eff_raw > 100.0 else f"{eff_raw:.0f}%"
    else:
        eff_display = "—"

    lines = [
        f"[dim]Game:[/] [cyan]{d.get('game', '?')}[/]  ·  "
        f"[dim]Strategy:[/] [cyan]{d.get('strategy', '?')}[/]",
        "",
        (f"[bold]Actual:[/] [magenta]{bps:.1f}[/] BPS (session)  ·  "
         f"[bold]Recent:[/] [magenta]{recent_bps:.1f}[/] BPS  ·  "
         f"[bold]Theoretical:[/] [magenta]{tmax:.1f}[/] BPS  ·  "
         f"[bold]Efficiency:[/] [magenta]{eff_display}[/]"),
        (f"[dim]Cycle:[/] median [cyan]{cycle_p50:.0f}ms[/]  p99 [cyan]{cycle_p99:.0f}ms[/]  "
         f"[dim](n={cycle_n} samples)[/]"),
        (f"[dim]Cycle breakdown:[/] api_p50=[cyan]{api_p50:.0f}ms[/] "
         f"+ overhead=[cyan]{overhead:.0f}ms[/] + delay=[cyan]{delay_ms:.0f}ms[/] "
         f"[dim]≈[/] [magenta]{api_p50 + overhead + delay_ms:.0f}ms[/]/bet [dim](decomposition)[/]"),
        (f"[dim]Measured cycle:[/]  [magenta]{cycle_ms:.0f}ms[/]/bet "
         f"[dim](median of last {cycle_n} iterations)[/]"),
    ]

    db_mode = d.get("db_write_mode") or "sync"
    dbw = d.get("db_writer_stats") or {}
    dbw_q   = int(d.get("db_writer_queue") or 0)
    dbw_fb  = int(dbw.get("fallback_sync") or 0)
    dbw_hwm = int(dbw.get("queue_high_watermark") or 0)
    if db_mode == "async":
        lines.append(
            f"[dim]DB writer:[/] mode=[cyan]async[/]  queue=[cyan]{dbw_q}/1000[/]  "
            f"batches=[cyan]{int(dbw.get('batches') or 0)}[/]  "
            f"flushed=[cyan]{int(dbw.get('flushed') or 0)}[/]  "
            f"fallback_sync=[{'red' if dbw_fb else 'cyan'}]{dbw_fb}[/]  hwm=[cyan]{dbw_hwm}[/]")
    else:
        lines.append("[dim]DB writer:[/] mode=[yellow]sync[/] (per-bet commit)")

    blockers = [("Network / API latency", api_p50),
                ("CLI overhead",          overhead),
                ("User-configured delay", delay_ms)]
    top_name, top_val = max(blockers, key=lambda x: x[1])

    advice = []
    if top_name == "Network / API latency" and api_p50 >= 100:
        advice.append("• Run [cyan]python3 shuffle.py --benchmark 20[/] to isolate the network path.")
        advice.append("• Consider a proxy in a region closer to shuffle.com, or move to a faster server.")
    elif top_name == "User-configured delay" and delay_ms > 10:
        advice.append(f"• You have bet_delay={bet_delay:.3f}s. Drop it with "
                      "[cyan]python3 shuffle.py --config-set delay=0[/] for max throughput.")
    elif top_name == "CLI overhead" and overhead > 30:
        if db_mode == "sync":
            advice.append("• CLI overhead high AND db_write_mode=sync — flip to async: "
                          "[cyan]python3 shuffle.py --config-set dbmode=async && shufflectl restart[/]")
        elif dbw_fb == 0 and dbw_hwm < 100:
            advice.append("• CLI overhead is high on this host.")
            advice.append("  The DB writer is healthy — overhead is likely shared-tenancy CPU/disk "
                          "jitter (VPS silicon), not a fixable code issue. Consider a fresh VPS "
                          "instance or a higher-tier plan.")
        else:
            advice.append("• CLI overhead is high. Check disk I/O (DB writes) and CPU load on the host.")
    if dbw_fb > 0:
        advice.append(f"• DB writer queue overflowed {dbw_fb} times "
                      "(async writer is saturating — check disk perf).")
    if not advice:
        advice.append("• Nothing obvious. Session is running near its natural ceiling.")

    lines += ["", f"[bold yellow]Top blocker:[/] {top_name} ([magenta]{top_val:.0f}ms[/])",
              "", "[bold]Advice:[/]"] + advice
    console.print(Panel("\n".join(lines), title="Diagnostic bottleneck analysis",
                        border_style="cyan"))


def main():
    parser = argparse.ArgumentParser(
        description="Shuffle AutoBot v" + VERSION + " -- Multi-Game",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  shuffle.py --config-show\n"
            "  shuffle.py --config-set maxprofit=0.5 delay=0.1\n"
            "  shuffle.py --benchmark 20\n"
            "  shuffle.py --diagnose\n"
            "  shuffle.py --cleanup 7\n"
            "  shuffle.py --config-set lanes=2 lane_split=budget lane2.max_wins=5\n"
            "  shuffle.py --daemon --resume --lane 2\n"
            "  shuffle.py --benchmark 20 --lanes 4\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"Shuffle AutoBot v{VERSION}")
    parser.add_argument("--resume",  action="store_true", help="Skip wizard, reuse saved config")
    parser.add_argument("--daemon",  action="store_true", help="Run in background (no TUI, implies --resume)")
    parser.add_argument("--setup-only", action="store_true", help="Run wizard, save config, don't start betting")
    parser.add_argument("--monitor", action="store_true", help="Attach live TUI to running daemon")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status",  action="store_true", help="Show status of running session")
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show all-time session statistics")
    group.add_argument("--analytics", action="store_true",
                       help="Show lifetime analytics: aggregates over all sessions with "
                            "breakdowns by game, strategy and lane. Complement to --stats.")
    group.add_argument("--last-bets", type=int, metavar="N", help="Show last N bets from database")
    group.add_argument("--session-bets", type=int, metavar="ID", help="Show bets for a specific session")
    group.add_argument("--config-show", action="store_true",
                       help="Print the saved config (token and proxy credentials masked)")
    group.add_argument("--config-set", nargs="+", metavar="KEY=VALUE",
                       help="Change saved config fields; a running daemon reloads live-safe ones via SIGHUP")
    group.add_argument("--benchmark", nargs="?", type=int, const=20, metavar="N",
                       help="Time N read-only GetMyProfile requests (default 20, 3-200); no bets")
    group.add_argument("--diagnose", action="store_true",
                       help="Bottleneck analysis of the running session (reads the state file)")
    group.add_argument("--cleanup", nargs="?", type=int, const=-1, metavar="DAYS",
                       help="Delete bet rows of sessions ended more than DAYS ago "
                            "(default: purge_days); session rows are kept")
    parser.add_argument("--since", metavar="DATE",
                        help="With --stats/--analytics: window start, server-local time "
                             "(YYYY-MM-DD, 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS')")
    parser.add_argument("--until", metavar="DATE",
                        help="With --stats/--analytics: window end, same formats "
                             "(bare date = inclusive end of day)")
    parser.add_argument("--lane", type=int, metavar="K", default=None,
                        help="Parallel lane to run or inspect (1-10, default 1): own state / pid "
                             "files and [LK] log prefix; with --stats/--analytics only that "
                             "lane's sessions")
    parser.add_argument("--lanes", type=int, metavar="M", default=None,
                        help="With --benchmark: also probe 1..M concurrent clients (2-10) "
                             "and recommend a lane count")
    parser.add_argument("--preset", type=str, metavar="NAME",
                        help="Load a named preset and start (skips wizard)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")

    args = parser.parse_args()

    # v1.10.0 — pick the lane before any command so --status / --stop / --monitor /
    # --diagnose act on that lane's files.
    try:
        _apply_lane(args.lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.daemon:
        args.resume = True

    if args.status:
        cmd_status()
        return
    if args.stop:
        cmd_stop()
        return
    if args.list_presets:
        cmd_list_presets()
        return
    if args.config_show:
        cmd_config_show()
        return
    if args.config_set:
        cmd_config_set(args.config_set)
        return
    if args.diagnose:
        cmd_diagnose()
        return
    if args.benchmark is not None:
        cmd_benchmark(args.benchmark, lanes=args.lanes)
        return
    if args.cleanup is not None:
        init_db()
        cmd_cleanup(None if args.cleanup == -1 else args.cleanup)
        return
    if args.stats:
        cmd_stats(since=args.since, until=args.until, lane=args.lane)
        return
    if args.analytics:
        cmd_analytics(since=args.since, until=args.until, lane=args.lane)
        return
    if args.monitor:
        cmd_monitor()
        return
    if args.last_bets:
        cmd_last_bets(args.last_bets)
        return
    if args.session_bets is not None:
        cmd_session_bets(args.session_bets)
        return

    if args.setup_only:
        init_db()
        setup_wizard()
        console.print("[bold green]Config saved.[/] Start with: [cyan]shufflectl start[/] or [cyan]shufflectl interactive[/]")
        return

    init_db()

    if args.preset:
        if not load_preset(args.preset):
            console.print(f"[red]Preset '{args.preset}' not found.[/]")
            cmd_list_presets()
            sys.exit(1)
        if not state.bearer_token:
            saved = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
            if saved.get("bearer_token"):
                state.bearer_token = saved["bearer_token"]
            else:
                console.print("[red]No bearer token found. Run wizard first.[/]")
                sys.exit(1)
        game_label = GAMES.get(state.game, {}).get("label", state.game)
        console.print(f"[green]Loaded preset '{args.preset}' -- {game_label} / {state.strategy} on "
                      f"{state.currency.upper()} @ {state.base_bet:.8f}[/]")
        try:
            api_test_connection()
        except Exception as e:
            detail = f": {e}" if APP_ENV != "production" else ""
            console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
            sys.exit(1)

    elif args.resume or args.daemon:
        _load_and_connect()
    else:
        setup_wizard()

    # v1.10.0 — lane share at start, then stagger lane K's first bet (K-1)·2 s.
    _init_lane_budget()
    if state.lane > 1:
        time.sleep((state.lane - 1) * 2.0)

    state.session_id    = _db_start_session()
    state.session_start = time.time()
    state.run_started_at = state.session_start
    state.initial_multiplier = state.multiplier_target
    state.running       = True

    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    logger.warning("SESSION START  id=%s  game=%s  currency=%s  strategy=%s  "
                   "multiplier=%s  base_bet=%.8f  balance=%.8f",
                   state.session_id, state.game,
                   state.currency, state.strategy,
                   state.multiplier_target, state.base_bet,
                   state.current_balance)

    def _shutdown(sig, frame):
        state.running = False
        state.status  = "Interrupted -- saving session..."
        logger.warning("SESSION END (signal)  bets=%d  profit=%+.8f  balance=%.8f",
                    state.total_bets, state.profit, state.current_balance)
        _stop_bet_writer()
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if hasattr(signal, "SIGUSR1"):
        def _sig_pause(sig, frame):
            state.paused = True
            state.status = "PAUSED (remote)"
        signal.signal(signal.SIGUSR1, _sig_pause)
    if hasattr(signal, "SIGUSR2"):
        def _sig_resume(sig, frame):
            state.paused = False
            state.status = "Resumed (remote)"
        signal.signal(signal.SIGUSR2, _sig_resume)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sig_reload)

    # v1.9.0 — retention + async writer (WolfBet v2.26.0 / v2.27.0)
    _startup_purge()
    if state.db_write_mode == "async":
        _start_bet_writer()
    threading.Thread(target=_retention_worker, daemon=True, name="Retention").start()

    t_bet = threading.Thread(target=betting_loop, daemon=True, name="BettingLoop")
    t_bet.start()

    def periodic_save():
        while state.running or state.paused:
            time.sleep(5)
            _db_save_session()
            _save_state_file()
    t_save = threading.Thread(target=periodic_save, daemon=True, name="PeriodicSave")
    t_save.start()

    # -- DAEMON MODE --
    if args.daemon:
        console.print(f"[bold green]Daemon started[/]  PID={os.getpid()}  Session #{state.session_id}  Game: {state.game}")
        console.print(f"[dim]  Check status:  shufflectl status[/]")
        console.print(f"[dim]  Stop:          shufflectl stop[/]")
        console.print(f"[dim]  Logs:          tail -f ~/.shuffle_logs/shuffle.log[/]")
        try:
            while state.running:
                time.sleep(2)
                _save_state_file()
        except KeyboardInterrupt:
            pass
        state.running = False
        _stop_bet_writer()
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        _print_summary()
        return

    # -- TUI MODE --
    t_input = threading.Thread(target=input_handler, daemon=True, name="InputHandler")
    t_input.start()

    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    sys.stdout.write("\033[?1049h")
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while state.running or state.paused:
            sys.stdout.write("\033[H")
            sys.stdout.write(build_dashboard_screen())
            sys.stdout.flush()
            time.sleep(0.5)

        sys.stdout.write("\033[H")
        sys.stdout.write(build_dashboard_screen())
        sys.stdout.flush()
        time.sleep(1.0)
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.write("\033[?1049l")
        sys.stdout.write("\033[0m")
        sys.stdout.flush()
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass
        sys.stdout.write("\033c")
        sys.stdout.flush()

    _stop_bet_writer()
    _db_save_session()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()
