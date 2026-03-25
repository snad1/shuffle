#!/usr/bin/env python3
"""
+----------------------------------------------------------+
|   Shuffle AutoBot v1.0  -- Multi-Game Auto-Betting Engine |
|   Limbo + Dice | Live TUI | Session logging & strategies  |
+----------------------------------------------------------+
"""

import os, sys, json, time, sqlite3, threading, signal, random, string, argparse, logging, re
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List
from logging.handlers import TimedRotatingFileHandler
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
        return cffi_requests.Session(impersonate="chrome")
    return requests.Session()

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
VERSION      = "1.0.0"
MIN_BET      = 0.001   # Shuffle minimum bet

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
    is_win = payout > 0
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
]

def save_config():
    data = {k: getattr(state, k) for k in _CONFIG_KEYS}
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)

def load_config() -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        for k in _CONFIG_KEYS:
            if k in data:
                setattr(state, k, data[k])
        state.current_bet = state.base_bet
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        if state.custom_rules_text:
            state.custom_rules = _load_rules_from_text(state.custom_rules_text)
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
]

def _load_all_presets() -> dict:
    if not os.path.exists(PRESET_PATH):
        return {}
    try:
        with open(PRESET_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_all_presets(presets: dict):
    with open(PRESET_PATH, "w") as f:
        json.dump(presets, f, indent=2)

def save_preset(name: str):
    presets = _load_all_presets()
    presets[name] = {k: getattr(state, k) for k in _PRESET_KEYS}
    _save_all_presets(presets)

def load_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    data = presets[name]
    for k in _PRESET_KEYS:
        if k in data:
            setattr(state, k, data[k])
    state.current_bet = state.base_bet
    if state.profit_threshold:
        state.next_profit_milestone = state.profit_threshold
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)
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

        self.base_bet         = MIN_BET
        self.current_bet      = MIN_BET
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


state   = BotState()
console = Console()

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

def _gql_post(query: str, variables: dict, operation_name: str) -> Optional[dict]:
    """Execute a GraphQL mutation/query. Retries once on connection reset."""
    global _http
    payload = {
        "operationName": operation_name,
        "query": query,
        "variables": variables,
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.4"}},
    }
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

def api_get_balances() -> list:
    """Fetch all balances via GetMyProfile query."""
    query = """query GetMyProfile {
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
    try:
        data = _gql_post(query, {}, "GetMyProfile")
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
    """Test auth with a zero-bet."""
    game_info = GAMES[state.game]
    build = game_info["build_query"]
    response_key = game_info["response_key"]

    saved_bet = state.current_bet
    state.current_bet = 0
    query, variables = build(state)
    state.current_bet = saved_bet

    try:
        data = _gql_post(query, variables, game_info["operation_name"])
        if response_key in data:
            return True
        raise Exception(f"Missing {response_key} in response")
    except Exception as e:
        raise Exception(f"Connection failed: {e}")

def api_place_bet(amount: float) -> Optional[dict]:
    """Place a bet via GraphQL. Returns parsed result dict or None."""
    if amount > 0 and amount < MIN_BET:
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
        data = _gql_post(query, variables, game_info["operation_name"])
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
        elif "insufficient" in err.lower() or "balance" in err.lower():
            logger.warning("INSUFFICIENT BALANCE")
            with state.lock:
                state.last_error = "Insufficient balance"
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
    return False, ""

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

        stop, reason = should_stop()
        if stop:
            state.running = False
            state.status  = f"STOPPED: {reason}"
            _db_save_session()
            break

        if state.bet_delay > 0:
            time.sleep(state.bet_delay)

        state.status = f"Placing {state.game} bet #{state.total_bets + 1}..."
        result = api_place_bet(state.current_bet)

        if result is None:
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

            if bet_state == "win":
                state.wins += 1
                if raw_profit > state.highest_win:
                    state.highest_win = raw_profit
                state.current_streak = max(state.current_streak, 0) + 1
                state.max_win_streak = max(state.max_win_streak, state.current_streak)
            else:
                state.losses += 1
                if amount_used > state.biggest_loss:
                    state.biggest_loss = amount_used
                state.current_streak = min(state.current_streak, 0) - 1
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
                state.bets_per_minute = state._bets_this_min

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

        _db_save_bet(result, raw_profit, new_balance)

        # Rule-Based: apply custom rules after bet
        if state.strategy_key == "7" and state.custom_rules:
            apply_rules(bet_state)

        # calculate next bet
        raw_next  = compute_next_bet(bet_state)
        next_bet  = max(state.base_bet, raw_next)
        if next_bet > 0 and next_bet < MIN_BET:
            next_bet = MIN_BET
        state.current_bet = next_bet
        last_result = bet_state

# ===========================================================
#  DATABASE I/O
# ===========================================================
def _db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _db_start_session() -> int:
    conn = _db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions
            (started_at, currency, game, strategy, base_bet, multiplier, start_balance)
        VALUES (?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        state.currency, state.game, state.strategy,
        state.base_bet, state.multiplier_target, state.start_balance,
    ))
    conn.commit()
    sid = c.lastrowid
    conn.close()
    return sid

def _db_save_bet(result: dict, profit: float, balance: float):
    try:
        conn = _db_conn()
        conn.execute("""
            INSERT INTO bets
                (session_id, timestamp, game, amount, multiplier_target,
                 result_value, result_display, state, profit, balance_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
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
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_bet failed: %s", e)

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
            datetime.now().isoformat(),
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
            "uptime_sec": int(time.time() - state.session_start),
            "status": state.status,
            "last_error": state.last_error,
            "updated_at": datetime.now().isoformat(),
        }
        with open(STATE_PATH, "w") as f:
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
    uptime = str(timedelta(seconds=int(time.time() - state.session_start)))
    if state.running and not state.paused:
        st_ind = _a("bold_green", "* LIVE")
    elif state.paused:
        st_ind = _a("bold_yellow", "* PAUSED")
    else:
        st_ind = _a("bold_red", "* STOPPED")

    game_info = _game_info_str()
    add(f"{_a('dim_magenta', '--- ')}{_a('bold_cyan', f'Shuffle v{VERSION}')}"
        f"{_a('white', f'  #{state.session_id}  {uptime}')}  {st_ind} "
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

    # -- Row 7: Separator --
    add(_a("dim_magenta", "-" * term_w))

    # -- Row 8: Bets table header --
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>10}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # -- Rows 9..N-2: Recent bet rows --
    bets_avail = max(0, term_h - 10)
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
    keys = "[P]ause [R]esume [Q]uit [H]istory"
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
        console.print(f"  [dim]Testing connection ({GAMES[state.game]['label']}, zero-bet)...[/]")
        try:
            api_test_connection()
            console.print("[green]  Connected![/]\n")
        except Exception as e:
            console.print(f"[red]  Connection failed: {e}[/]")
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
                    console.print("    [bold yellow][1][/] Add rule   [bold yellow][2][/] Delete rule   [bold yellow][3][/] Clear all   [bold yellow][4][/] Continue")
                    v2 = _ask("  Select", default="4", choices=["1", "2", "3", "4"])
                    if v2 is _BACK:
                        return False
                    if v2 == "4":
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
                    elif v2 == "3":
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
             step_stop_conditions, step_confirm]
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
def cmd_stats():
    init_db()
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()
    rows = conn.execute("""
        SELECT id, started_at, ended_at, UPPER(currency), COALESCE(game, 'limbo'),
               strategy, total_bets, wins, losses, profit, wagered,
               start_balance, end_balance, max_win_streak, max_loss_streak,
               COALESCE(bets_per_second, 0)
        FROM sessions ORDER BY id DESC LIMIT 20
    """).fetchall()
    if not rows:
        console.print("[dim]No sessions found.[/]")
        conn.close()
        return
    console.print(Rule("[bold cyan]Session History (last 20)[/]"))
    for r in rows:
        sid, started, ended, cur, game, strat, bets, wins, losses, profit, wagered, \
            start_bal, end_bal, mws, mls, bps = r
        bets = bets or 0; profit = profit or 0
        pc = "green" if profit >= 0 else "red"
        ps = "+" if profit >= 0 else ""
        wr = f"{wins/bets*100:.1f}%" if bets > 0 else "--"
        game_label = GAMES.get(game, {}).get("label", game or "?")
        console.print(f"  [bold cyan]#{sid}[/]  {game_label}/{(cur or '?')}  {strat or '?'}  "
                      f"Bets: {bets}  [{pc}]{ps}{profit:.8f}[/]  WR: {wr}  W+{mws or 0}/L-{mls or 0}")
    conn.close()
    console.print()

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
    console.print(Rule(f"[bold cyan]Shuffle Session #{d.get('session_id', '?')}  {status}[/]"))
    console.print(f"  [dim]Uptime:[/]   {timedelta(seconds=d.get('uptime_sec', 0))}")
    console.print(f"  [dim]Bets:[/]     {d.get('total_bets', 0)}  W: {d.get('wins', 0)} / L: {d.get('losses', 0)}")
    console.print(f"  [dim]Profit:[/]   [{pc}]{ps}{p:.8f}[/]")
    console.print(f"  [dim]Balance:[/]  {d.get('current_balance', 0):.8f}")
    console.print(f"  [dim]Status:[/]   {d.get('status', '')[:80]}")
    console.print()

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
        console.print(f"[red]Error: {e}[/]")

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

    if pid_alive and not paused:
        st_ind = _a("bold_green", "* LIVE")
    elif pid_alive and paused:
        st_ind = _a("bold_yellow", "* PAUSED")
    else:
        st_ind = _a("bold_red", "* STOPPED")

    add(f"{_a('dim_magenta', '--- ')}{_a('bold_cyan', f'Shuffle v{VERSION}')}"
        f"{_a('white', f'  #{sid}  {uptime}')}  {st_ind} "
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

    add(f" {_a('dim', 'P/L')} {_a('dim', '—')}")

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

            bet_limit = max(0, (os.get_terminal_size().lines if sys.stdout.isatty() else 24) - 10)
            recent_bets = _monitor_get_recent_bets(sid, bet_limit)

            sys.stdout.write("\033[H")
            sys.stdout.write(_build_monitor_screen(d, recent_bets, bool(pid)))
            sys.stdout.flush()
            time.sleep(0.5)

        if _monitor_action[0] == "stop" or (locals().get("action") == "stop"):
            if pid:
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
        console.print(f"[red]Connection failed: {e}[/]")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Shuffle AutoBot v" + VERSION + " -- Multi-Game")
    parser.add_argument("--resume",  action="store_true", help="Skip wizard, reuse saved config")
    parser.add_argument("--daemon",  action="store_true", help="Run in background (no TUI, implies --resume)")
    parser.add_argument("--setup-only", action="store_true", help="Run wizard, save config, don't start betting")
    parser.add_argument("--monitor", action="store_true", help="Attach live TUI to running daemon")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status",  action="store_true", help="Show status of running session")
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show all-time session statistics")
    group.add_argument("--last-bets", type=int, metavar="N", help="Show last N bets from database")
    group.add_argument("--session-bets", type=int, metavar="ID", help="Show bets for a specific session")
    parser.add_argument("--preset", type=str, metavar="NAME",
                        help="Load a named preset and start (skips wizard)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")

    args = parser.parse_args()

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
    if args.stats:
        cmd_stats()
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
            console.print(f"[red]Connection failed: {e}[/]")
            sys.exit(1)

    elif args.resume or args.daemon:
        _load_and_connect()
    else:
        setup_wizard()

    state.session_id    = _db_start_session()
    state.session_start = time.time()
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

    _db_save_session()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()
