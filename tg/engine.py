"""BettingEngine — per-user betting instance running in its own thread.

Key features:
  - Cross-thread SQLite safety (temp conn for session start, lazy persistent in betting thread)
  - Batched DB writes (flush every 50 bets + save session stats on each flush)
  - _db_save_session(final=False) for periodic saves without setting ended_at
  - Multi-game support via game registry (Limbo + Dice)
  - Bearer token auth (no Cloudflare bypass needed)
  - Profit-based base bet increment
  - Milestone callbacks
  - Full ISO timestamps with microseconds
"""

import json
import time
import random
import string
import sqlite3
import threading
from datetime import datetime
from collections import deque
from typing import Optional, List

from .config import API_URL, MIN_BET, logger
from core.database import init_db, db_connect, cleanup_old_bets
from core.strategy import StrategyRule, load_rules_from_text
from core.engine import compute_next_bet, apply_action, evaluate_rules

# Try curl_cffi for TLS fingerprint bypass on servers; fall back to requests
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

import requests

# ── Batching / periodic save constants ──────────────────
BET_BATCH_SIZE     = 50      # flush bets + session stats to DB every N bets
SESSION_SAVE_SECS  = 30      # fallback: save session stats every N seconds
REQUEST_TIMEOUT    = (5, 15) # (connect, read) in seconds
MAX_TIMEOUT_STREAK = 3       # recreate HTTP session after this many consecutive timeouts


def _gen_window_id() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=21))


# ── GraphQL queries / mutations ──────────────────────────
_LIMBO_QUERY = """mutation LimboPlay($amount: Float!, $bet: Float!, $currency: String!, $windowId: String!, $usdAmount: Float) {
  limbo(amount: $amount, bet: $bet, currency: $currency, windowId: $windowId, usdAmount: $usdAmount) {
    ...CasinoGameFields
    shuffleOriginalActions {
      action {
        ... on CasinoGameLimbo {
          limbo {
            result
            __typename
          }
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment CasinoGameFields on CasinoGame {
  id
  active
  payoutMultiplier
  amountMultiplier
  amount
  payout
  updatedAt
  currency
  game
  afterBalance
  __typename
}"""

_DICE_QUERY = """mutation DicePlay($amount: Float!, $bet: Float!, $currency: String!, $direction: String!, $usdAmount: Float) {
  dice(amount: $amount, bet: $bet, currency: $currency, direction: $direction, usdAmount: $usdAmount) {
    ...CasinoGameFields
    shuffleOriginalActions {
      action {
        ... on CasinoGameDice {
          dice {
            ...DiceFields
            __typename
          }
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment CasinoGameFields on CasinoGame {
  id
  active
  payoutMultiplier
  amountMultiplier
  amount
  payout
  updatedAt
  currency
  game
  afterBalance
  __typename
}

fragment DiceFields on Dice {
  result
  target
  condition
  __typename
}"""

_BALANCE_QUERY = """query GetMyProfile {
  me {
    id
    account {
      balances {
        currency
        availableAmount
        __typename
      }
      __typename
    }
    __typename
  }
}"""


# ── Game registry ────────────────────────────────────────
def _limbo_build(engine):
    return {
        "operationName": "LimboPlay",
        "query": _LIMBO_QUERY,
        "variables": {
            "amount": round(engine.current_bet, 8) if engine.current_bet >= MIN_BET else engine.current_bet,
            "bet": engine.multiplier_target,
            "currency": engine.currency,
            "windowId": _gen_window_id(),
            "usdAmount": 0,
        },
    }


def _limbo_parse(data, engine=None):
    limbo_data = data.get("limbo", data)
    payout_mult = float(limbo_data.get("payoutMultiplier", 0))
    amount = float(limbo_data.get("amount", 0))
    payout = float(limbo_data.get("payout", 0))
    after_balance = limbo_data.get("afterBalance")

    # Get result from nested shuffleOriginalActions
    result = 0.0
    actions = limbo_data.get("shuffleOriginalActions", [])
    if actions:
        action = actions[0].get("action", {})
        limbo_inner = action.get("limbo", {})
        result = float(limbo_inner.get("result", 0))

    target = engine.multiplier_target if engine else 0
    is_win = result >= target if target > 0 else payout_mult > 0

    return {
        "amount": amount,
        "payout": payout,
        "payout_mult": payout_mult,
        "result_display": f"{result:.4f}x",
        "result_value": result,
        "is_win": is_win,
        "after_balance": float(after_balance) if after_balance is not None else None,
    }


def _dice_build(engine):
    direction = "ABOVE" if engine.dice_condition == "above" else "BELOW"
    return {
        "operationName": "DicePlay",
        "query": _DICE_QUERY,
        "variables": {
            "amount": round(engine.current_bet, 8) if engine.current_bet >= MIN_BET else engine.current_bet,
            "bet": engine.dice_target,
            "currency": engine.currency,
            "direction": direction,
            "usdAmount": 0,
        },
    }


def _dice_parse(data, engine=None):
    dice_data = data.get("dice", data)
    payout_mult = float(dice_data.get("payoutMultiplier", 0))
    amount = float(dice_data.get("amount", 0))
    payout = float(dice_data.get("payout", 0))
    after_balance = dice_data.get("afterBalance")

    result = 0.0
    target = 0.0
    condition = ""
    actions = dice_data.get("shuffleOriginalActions", [])
    if actions:
        action = actions[0].get("action", {})
        dice_inner = action.get("dice", {})
        result = float(dice_inner.get("result", 0))
        target = float(dice_inner.get("target", 0))
        condition = dice_inner.get("condition", "")

    if condition and target > 0:
        is_win = result > target if condition.lower() == "above" else result < target
    else:
        is_win = payout_mult > 0

    return {
        "amount": amount,
        "payout": payout,
        "payout_mult": payout_mult,
        "result_display": f"{result:.2f}",
        "result_value": result,
        "is_win": is_win,
        "after_balance": float(after_balance) if after_balance is not None else None,
    }


GAMES = {
    "limbo": {
        "label": "Limbo",
        "response_key": "limbo",
        "build_payload": _limbo_build,
        "parse_result": _limbo_parse,
    },
    "dice": {
        "label": "Dice",
        "response_key": "dice",
        "build_payload": _dice_build,
        "parse_result": _dice_parse,
    },
}


def _cfg(config: dict, key: str, default, typ=float):
    """Get config value; treat None as default but allow 0."""
    v = config.get(key)
    return typ(v) if v is not None else typ(default)


class BettingEngine:
    """Self-contained betting engine for one user. Runs in its own thread."""

    def __init__(self, user_id: int, db_path: str, config: dict):
        self.user_id = user_id
        self.db_path = db_path
        self.lock    = threading.Lock()

        # ── auth ──
        self.bearer_token = config.get("bearer_token") or ""

        # ── game config ──
        self.game              = config.get("game") or "limbo"
        self.currency          = config.get("currency") or "usdt"
        self.multiplier_target = _cfg(config, "multiplier_target", 2.0)
        self.initial_multiplier = self.multiplier_target
        self.dice_target       = _cfg(config, "dice_target", 50.5)
        self.dice_condition    = config.get("dice_condition") or "above"
        self.base_bet          = _cfg(config, "base_bet", 0.001)
        self.current_bet       = self.base_bet
        self.strategy          = config.get("strategy") or "Martingale"
        self.strategy_key      = config.get("strategy_key") or "2"
        self.win_mult          = _cfg(config, "win_mult", 1.0)
        self.loss_mult         = _cfg(config, "loss_mult", 2.0)
        self.bet_delay         = _cfg(config, "bet_delay", 0)
        self.delay_martin_threshold = _cfg(config, "delay_martin_threshold", 3, int)

        # ── rules ──
        self.custom_rules: List[StrategyRule] = []
        rules_text = config.get("custom_rules_text", "")
        if rules_text:
            self.custom_rules = load_rules_from_text(rules_text)

        # ── stop conditions ──
        self.max_profit      = config.get("max_profit")
        self.max_loss        = config.get("max_loss")
        self.max_bets        = config.get("max_bets")
        self.max_wins        = config.get("max_wins")
        self.stop_on_balance = config.get("stop_on_balance")

        # ── session state ──
        self.running         = False
        self.paused          = False
        self.session_id      = None
        self.session_start   = 0.0
        self.total_bets      = 0
        self.wins            = 0
        self.losses          = 0
        self.profit          = 0.0
        self.wagered         = 0.0
        self.start_balance   = 0.0
        self.current_balance = 0.0
        self.current_streak  = 0
        self.max_win_streak  = 0
        self.max_loss_streak = 0
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")
        self.bets_per_second = 0.0
        self.bets_per_minute = 0.0
        self.peak_bps        = 0.0
        self.low_bps         = float("inf")
        self.peak_bpm        = 0.0
        self.low_bpm         = float("inf")
        self._bets_this_sec  = 0
        self._bets_this_min  = 0
        self._current_sec    = 0
        self._current_min    = 0
        self.profit_history  = deque(maxlen=40)
        self.profit_history.append(0.0)
        self.recent_bets     = deque(maxlen=5)
        self.status          = "Idle"
        self.last_error      = ""
        self.stop_reason     = ""

        # ── error backoff ──
        self.consecutive_errors = 0
        self._consecutive_timeouts = 0
        self.backoff_delay   = 1.0
        self._insufficient_balance = False
        self._last_api_ms          = 0.0
        self._api_ms_total         = 0.0
        self._api_ms_count         = 0

        # ── strategy internals ──
        self.dalembert_unit  = 0
        self.paroli_count    = 0

        # ── profit-based base bet increment ──
        self.profit_increment     = config.get("profit_increment")
        self.profit_threshold     = config.get("profit_threshold")
        self.next_profit_milestone = float(self.profit_threshold) if self.profit_threshold else 0.0

        # ── milestone config ──
        self.milestone_bets   = _cfg(config, "milestone_bets", 100, int)
        self.milestone_wins   = _cfg(config, "milestone_wins", 0, int)
        self.milestone_losses = _cfg(config, "milestone_losses", 0, int)
        self.milestone_profit = _cfg(config, "milestone_profit", 0)
        self._last_profit_milestone = 0.0

        # ── callbacks ──
        self.on_stop         = None
        self.on_milestone    = None
        self.on_error        = None

        # ── batching state ──
        self._bet_queue: list = []
        self._last_session_save = 0.0
        self._db_connection: Optional[sqlite3.Connection] = None

        # ── HTTP session ──
        self._http = None

        # ── thread ──
        self._thread: Optional[threading.Thread] = None

    # ── HTTP ─────────────────────────────────────────────
    def _init_http(self):
        if _HAS_CFFI:
            self._http = cffi_requests.Session(impersonate="chrome")
        else:
            self._http = requests.Session()

    def _recreate_http(self):
        try:
            if self._http:
                self._http.close()
        except Exception:
            pass
        self._init_http()
        logger.info("User %d: HTTP session recreated", self.user_id)

    def _headers(self) -> dict:
        import uuid
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Authorization": f"Bearer {self.bearer_token}",
            "Origin": "https://shuffle.com",
            "Referer": "https://shuffle.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
            "x-correlation-id": str(uuid.uuid4()),
        }

    def _gql_post(self, payload: dict) -> Optional[dict]:
        for attempt in range(2):
            try:
                r = self._http.post(API_URL, headers=self._headers(), json=payload, timeout=REQUEST_TIMEOUT)
                break
            except (ConnectionError, ConnectionResetError, requests.exceptions.ConnectionError) as e:
                if attempt == 0:
                    logger.warning("User %d: Connection reset — recreating HTTP session", self.user_id)
                    self._recreate_http()
                    continue
                raise ConnectionError(f"Connection failed: {e}")
        if r.status_code != 200:
            body = r.text[:300] if r.text else "(empty)"
            raise ConnectionError(f"HTTP {r.status_code}: {body}")
        return r.json()

    # ── API ──────────────────────────────────────────────
    def api_get_balances(self) -> list:
        """Fetch all balances via GraphQL."""
        payload = {
            "operationName": "GetMyProfile",
            "query": _BALANCE_QUERY,
            "variables": {},
        }
        try:
            data = self._gql_post(payload)
            me = data.get("data", {}).get("me", {})
            account = me.get("account", {})
            balances = account.get("balances", [])
            return [
                {"currency": b.get("currency", ""), "amount": float(b.get("availableAmount", 0))}
                for b in balances if float(b.get("availableAmount", 0)) > 0
            ]
        except Exception as e:
            logger.warning("Balance fetch failed: %s", e)
        return []

    def _api_test_connection(self) -> bool:
        """Test auth by fetching the user profile (read-only, no bet placed)."""
        try:
            balances = self.api_get_balances()
            if isinstance(balances, list):
                return True
            self.last_error = "Unexpected response from profile query"
            return False
        except Exception as e:
            self.last_error = str(e)[:200]
            return False

    def _api_place_bet(self, amount: float) -> Optional[dict]:
        """Place a bet. Returns parsed result dict or None."""
        if 0 < amount < MIN_BET:
            amount = MIN_BET

        game_info = GAMES[self.game]
        resp_key = game_info["response_key"]
        build = game_info["build_payload"]
        parse = game_info["parse_result"]

        saved = self.current_bet
        self.current_bet = amount
        payload = build(self)
        self.current_bet = saved

        try:
            t0 = time.time()
            data = self._gql_post(payload)
            self._last_api_ms = (time.time() - t0) * 1000
            self._consecutive_timeouts = 0

            if data is None:
                self.last_error = "Empty response"
                return None

            # Check for GraphQL errors
            errors = data.get("errors")
            if errors:
                err_str = str(errors).lower()
                if "insufficient" in err_str or "balance" in err_str:
                    self.last_error = "Insufficient balance"
                    self._insufficient_balance = True
                    return None
                self.last_error = f"API error: {str(errors)[:120]}"
                return None

            gql_data = data.get("data", {})
            raw = gql_data.get(resp_key)
            if not raw:
                self.last_error = f"Empty {resp_key}"
                return None

            self._api_ms_total += self._last_api_ms
            self._api_ms_count += 1
            return parse(raw, self)
        except ConnectionError as e:
            err = str(e)
            if "429" in err:
                self.last_error = "Rate limit 429"
            elif "insufficient" in err.lower() or "balance" in err.lower():
                self.last_error = "Insufficient balance"
                self._insufficient_balance = True
            else:
                self.last_error = err[:120]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            self._consecutive_timeouts += 1
            self.last_error = f"Request timeout (#{self._consecutive_timeouts})"
            if self._consecutive_timeouts >= MAX_TIMEOUT_STREAK:
                logger.warning("User %d: %d consecutive timeouts — recreating HTTP session",
                               self.user_id, self._consecutive_timeouts)
                self._recreate_http()
                self._consecutive_timeouts = 0
        except Exception as e:
            self.last_error = str(e)[:120]
        return None

    # ── DB ───────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        if self._db_connection is None:
            self._db_connection = db_connect(self.db_path)
        return self._db_connection

    def _close_conn(self):
        if self._db_connection:
            try:
                self._db_connection.close()
            except Exception:
                pass
            self._db_connection = None

    def _build_config_snapshot(self) -> str:
        snap = {
            "strategy_key": self.strategy_key,
            "loss_mult": self.loss_mult,
            "win_mult": self.win_mult,
            "bet_delay": self.bet_delay,
        }
        if self.strategy_key == "6":
            snap["delay_threshold"] = self.delay_martin_threshold
        if self.game == "dice":
            snap["dice_target"] = self.dice_target
            snap["dice_condition"] = self.dice_condition
        stops = {}
        if self.max_profit is not None: stops["max_profit"] = self.max_profit
        if self.max_loss is not None:   stops["max_loss"] = self.max_loss
        if self.max_bets is not None:   stops["max_bets"] = self.max_bets
        if self.max_wins is not None:   stops["max_wins"] = self.max_wins
        if self.stop_on_balance is not None: stops["min_balance"] = self.stop_on_balance
        if stops:
            snap["stops"] = stops
        if self.profit_threshold and self.profit_increment:
            snap["profit_threshold"] = self.profit_threshold
            snap["profit_increment"] = self.profit_increment
        if self.custom_rules:
            snap["rules"] = [r.to_dict() for r in self.custom_rules]
        return json.dumps(snap)

    def _db_start_session(self) -> int:
        snapshot = self._build_config_snapshot()
        conn = db_connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO sessions (started_at, currency, game, strategy, base_bet, multiplier,
                                  start_balance, config_snapshot)
            VALUES (?,?,?,?,?,?,?,?)
        """, (datetime.now().isoformat(), self.currency, self.game,
              self.strategy, self.base_bet, self.multiplier_target, self.start_balance,
              snapshot))
        conn.commit()
        sid = c.lastrowid
        conn.close()
        return sid

    def _queue_bet(self, result: dict, profit: float, balance: float):
        self._bet_queue.append((
            self.session_id, datetime.now().isoformat(),
            self.game,
            float(result.get("amount", 0)),
            self.multiplier_target,
            float(result.get("result_value", 0)),
            result.get("result_display", ""),
            "win" if result.get("is_win") else "loss",
            profit, balance,
        ))
        if len(self._bet_queue) >= BET_BATCH_SIZE:
            self._flush_bets()

    def _flush_bets(self):
        if not self._bet_queue:
            return
        try:
            conn = self._get_conn()
            conn.executemany("""
                INSERT INTO bets (session_id, timestamp, game, amount, multiplier_target,
                    result_value, result_display, state, profit, balance_after)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, self._bet_queue)
            conn.commit()
            self._bet_queue.clear()
        except Exception as e:
            logger.error("User %d: DB flush_bets failed: %s", self.user_id, e)
        self._db_save_session()

    def _db_save_session(self, final: bool = False):
        try:
            conn = self._get_conn()
            lo_bal = self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance
            fields = (
                self.total_bets, self.wins, self.losses,
                self.profit, self.wagered, self.current_balance,
                self.max_win_streak, self.max_loss_streak,
                self.highest_balance, lo_bal,
                self.highest_win, self.biggest_loss,
                self.bets_per_minute,
                self.bets_per_second,
                self.peak_bps,
                self.low_bps if self.low_bps != float("inf") else 0,
                self.peak_bpm,
                self.low_bpm if self.low_bpm != float("inf") else 0,
                self.session_id,
            )
            if final:
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
                """, (datetime.now().isoformat(),) + fields)
            else:
                conn.execute("""
                    UPDATE sessions SET
                        total_bets=?, wins=?, losses=?,
                        profit=?, wagered=?, end_balance=?,
                        max_win_streak=?, max_loss_streak=?,
                        highest_balance=?, lowest_balance=?,
                        highest_win=?, biggest_loss=?,
                        bets_per_minute=?, bets_per_second=?,
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
                    WHERE id=?
                """, fields)
            conn.commit()
            self._last_session_save = time.time()
        except Exception as e:
            logger.error("User %d: DB save_session failed: %s", self.user_id, e)

    def _periodic_save(self):
        now = time.time()
        if now - self._last_session_save >= SESSION_SAVE_SECS:
            self._flush_bets()
            self._db_save_session()

    # ── STRATEGY (delegates to core.engine) ────────────────
    def _compute_next_bet(self, last_result: str) -> float:
        nxt, self.dalembert_unit, self.paroli_count = compute_next_bet(
            self.strategy_key, self.base_bet, self.current_bet,
            self.loss_mult, self.win_mult, last_result,
            self.current_streak, self.delay_martin_threshold,
            self.dalembert_unit, self.paroli_count,
        )
        return nxt

    def _apply_rules(self, bet_state: str):
        triggered = evaluate_rules(
            self.custom_rules, bet_state, self.wins, self.losses,
            self.total_bets, self.current_streak, self.profit,
            self.current_balance, self.current_bet, self.multiplier_target,
        )
        for rule in triggered:
            mutations = apply_action(
                rule, self.current_bet, self.base_bet,
                self.dice_condition, self.multiplier_target, self.current_streak,
                initial_multiplier=self.initial_multiplier,
            )
            for k, v in mutations.items():
                setattr(self, k, v)

    def _should_stop(self):
        p = self.profit
        b = self.current_balance
        if self.max_profit is not None and p >= self.max_profit:
            return True, f"Profit target: {p:+.8f}"
        if self.max_loss is not None and p <= -abs(self.max_loss):
            return True, f"Max loss: {p:+.8f}"
        if self.max_bets is not None and self.total_bets >= self.max_bets:
            return True, f"Max bets: {self.total_bets}"
        if self.max_wins is not None and self.wins >= self.max_wins:
            return True, f"Max wins: {self.wins}"
        if self.stop_on_balance is not None and b <= self.stop_on_balance:
            return True, f"Balance floor: {b:.8f}"
        return False, ""

    def _check_milestone(self, last_state: str) -> str:
        if self.milestone_bets > 0 and self.total_bets % self.milestone_bets == 0:
            return f"{self.total_bets} bets"
        if self.milestone_wins > 0 and last_state == "win" and self.wins % self.milestone_wins == 0:
            return f"{self.wins} wins"
        if self.milestone_losses > 0 and last_state != "win" and self.losses % self.milestone_losses == 0:
            return f"{self.losses} losses"
        if self.milestone_profit > 0 and self.profit > 0:
            threshold = self.milestone_profit
            current_level = int(self.profit / threshold)
            last_level = int(self._last_profit_milestone / threshold)
            if current_level > last_level:
                self._last_profit_milestone = self.profit
                return f"profit +{self.profit:.8f}"
        return ""

    # ── LIFECYCLE ────────────────────────────────────────
    def start(self) -> bool:
        """Connect, fetch balance, start betting thread. Returns True on success."""
        self._init_http()

        if not self._api_test_connection():
            return False

        try:
            balances = self.api_get_balances()
        except Exception as e:
            self.last_error = f"Balance fetch failed: {e}"
            return False

        bal = 0.0
        for b in balances:
            if b.get("currency", "").lower() == self.currency.lower():
                bal = float(b.get("amount", 0))
                break
        if bal <= 0:
            self.last_error = f"No {self.currency.upper()} balance found"
            return False

        init_db(self.db_path)
        cleanup_old_bets(self.db_path)
        self.start_balance   = bal
        self.current_balance = bal
        self.highest_balance = bal
        self.lowest_balance  = bal
        self.session_start   = time.time()
        self._last_session_save = time.time()
        self.session_id      = self._db_start_session()
        self.running         = True
        self.status          = "Starting..."

        self._thread = threading.Thread(target=self._betting_loop, daemon=True)
        self._thread.start()
        return True

    # ── RESUME SUPPORT ────────────────────────────────────
    def snapshot_state(self) -> dict:
        rules_ser = json.dumps([r.to_dict() for r in self.custom_rules]) if self.custom_rules else ""
        config = {
            "bearer_token": self.bearer_token,
            "game": self.game,
            "currency": self.currency,
            "multiplier_target": self.multiplier_target,
            "dice_target": self.dice_target,
            "dice_condition": self.dice_condition,
            "base_bet": self.base_bet,
            "strategy": self.strategy,
            "strategy_key": self.strategy_key,
            "win_mult": self.win_mult,
            "loss_mult": self.loss_mult,
            "bet_delay": self.bet_delay,
            "delay_martin_threshold": self.delay_martin_threshold,
            "custom_rules_text": rules_ser,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "max_bets": self.max_bets,
            "max_wins": self.max_wins,
            "stop_on_balance": self.stop_on_balance,
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "milestone_bets": self.milestone_bets,
            "milestone_wins": self.milestone_wins,
            "milestone_losses": self.milestone_losses,
            "milestone_profit": self.milestone_profit,
        }
        return {
            "user_id": self.user_id,
            "db_path": self.db_path,
            "config": config,
            "session_id": self.session_id,
            "session_start": self.session_start,
            "initial_multiplier": self.initial_multiplier,
            "current_bet": self.current_bet,
            "total_bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "profit": self.profit,
            "wagered": self.wagered,
            "start_balance": self.start_balance,
            "current_balance": self.current_balance,
            "current_streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_bet": self.highest_bet,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "highest_balance": self.highest_balance,
            "lowest_balance": self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance,
            "dalembert_unit": self.dalembert_unit,
            "paroli_count": self.paroli_count,
            "next_profit_milestone": self.next_profit_milestone,
            "_last_profit_milestone": self._last_profit_milestone,
        }

    def restore_state(self, snap: dict):
        self.session_id        = snap["session_id"]
        self.session_start     = snap["session_start"]
        self.initial_multiplier = snap.get("initial_multiplier", self.multiplier_target)
        self.current_bet       = snap["current_bet"]
        self.total_bets        = snap["total_bets"]
        self.wins              = snap["wins"]
        self.losses            = snap["losses"]
        self.profit            = snap["profit"]
        self.wagered           = snap["wagered"]
        self.start_balance     = snap["start_balance"]
        self.current_balance   = snap["current_balance"]
        self.current_streak    = snap["current_streak"]
        self.max_win_streak    = snap["max_win_streak"]
        self.max_loss_streak   = snap["max_loss_streak"]
        self.highest_bet       = snap["highest_bet"]
        self.highest_win       = snap["highest_win"]
        self.biggest_loss      = snap["biggest_loss"]
        self.highest_balance   = snap["highest_balance"]
        self.lowest_balance    = snap.get("lowest_balance", float("inf"))
        self.dalembert_unit    = snap.get("dalembert_unit", 0)
        self.paroli_count      = snap.get("paroli_count", 0)
        self.next_profit_milestone  = snap.get("next_profit_milestone", 0.0)
        self._last_profit_milestone = snap.get("_last_profit_milestone", 0.0)

    def start_resumed(self) -> bool:
        self._init_http()
        init_db(self.db_path)

        if not self._api_test_connection():
            self.last_error = f"Resume connection failed: {self.last_error}"
            return False

        self.running = True
        self.paused = False
        self.status = "Resumed"
        self._last_session_save = time.time()
        elapsed = time.time() - self.session_start if self.session_start else 0
        if elapsed > 0 and self.total_bets > 0:
            self.bets_per_second = self.total_bets / elapsed
            self.bets_per_minute = self.bets_per_second * 60
        self._thread = threading.Thread(target=self._betting_loop, daemon=True)
        self._thread.start()
        logger.info("User %d: Resumed session #%d", self.user_id, self.session_id)
        return True

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def get_status(self) -> dict:
        elapsed = time.time() - self.session_start if self.session_start else 0
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        wr = f"{self.wins/self.total_bets*100:.1f}%" if self.total_bets > 0 else "\u2014"
        # game info
        if self.game == "dice":
            game_info = f"Dice {self.dice_condition} {self.dice_target}"
        else:
            game_info = self.game.capitalize()

        # stop conditions
        stops = {}
        if self.max_profit is not None: stops["max_profit"] = self.max_profit
        if self.max_loss is not None:   stops["max_loss"] = self.max_loss
        if self.max_bets is not None:   stops["max_bets"] = self.max_bets
        if self.max_wins is not None:   stops["max_wins"] = self.max_wins
        if self.stop_on_balance is not None: stops["min_balance"] = self.stop_on_balance

        # strategy detail
        strat_detail = ""
        if self.strategy_key in ("2", "6", "7"):
            strat_detail = f"loss_mult: {self.loss_mult}x"
        elif self.strategy_key in ("3", "5"):
            strat_detail = f"win_mult: {self.win_mult}x"

        # rule descriptions
        rule_descs = []
        for r in self.custom_rules:
            d = r.to_dict()
            rule_descs.append(f"{d.get('trigger','')} {d.get('condition','')} {d.get('threshold','')}: {d.get('action','')} {d.get('value','')}")

        return {
            "running": self.running,
            "paused": self.paused,
            "session_id": self.session_id,
            "uptime": f"{h}h {m}m {s}s",
            "bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": wr,
            "profit": self.profit,
            "wagered": self.wagered,
            "balance": self.current_balance,
            "start_balance": self.start_balance,
            "current_bet": self.current_bet,
            "highest_bet": self.highest_bet,
            "streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_balance": self.highest_balance,
            "lowest_balance": self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "bps": self.bets_per_second,
            "bpm": self.bets_per_minute,
            "peak_bps": self.peak_bps,
            "low_bps": self.low_bps if self.low_bps != float("inf") else 0,
            "peak_bpm": self.peak_bpm,
            "low_bpm": self.low_bpm if self.low_bpm != float("inf") else 0,
            "base_bet": self.base_bet,
            "strategy": self.strategy,
            "strategy_key": self.strategy_key,
            "strategy_detail": strat_detail,
            "game": self.game,
            "game_info": game_info,
            "multiplier": self.multiplier_target,
            "currency": self.currency.upper(),
            "bet_delay": self.bet_delay,
            "stop_conditions": stops,
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "rules": rule_descs,
            "status": self.status,
            "last_error": self.last_error,
            "api_ms": self._last_api_ms,
            "api_avg_ms": (self._api_ms_total / self._api_ms_count) if self._api_ms_count > 0 else 0,
        }

    # ── BETTING LOOP ─────────────────────────────────────
    def _betting_loop(self):
        last_result = "none"
        logger.info("User %d: Session #%d started (%s %s)",
                     self.user_id, self.session_id, self.game, self.strategy)

        while self.running:
            if self.paused:
                self.status = "PAUSED"
                time.sleep(0.5)
                continue

            stop, reason = self._should_stop()
            if stop:
                self.running = False
                self.stop_reason = reason
                self.status = f"STOPPED: {reason}"
                self._flush_bets()
                self._db_save_session(final=True)
                break

            if self.bet_delay > 0:
                time.sleep(self.bet_delay)

            self.status = f"Placing bet #{self.total_bets + 1}..."
            result = self._api_place_bet(self.current_bet)

            if result is None:
                if self._insufficient_balance:
                    if self.on_error:
                        try:
                            self.on_error(f"Insufficient balance for bet {self.current_bet:.8f}")
                        except Exception:
                            pass
                    self.running = False
                    self.stop_reason = f"Insufficient balance for bet {self.current_bet:.8f}"
                    self.status = f"STOPPED: {self.stop_reason}"
                    self._flush_bets()
                    self._db_save_session(final=True)
                    break
                self.consecutive_errors += 1
                self.backoff_delay = min(self.backoff_delay * 2, 30.0)
                time.sleep(self.backoff_delay)
                continue

            is_win       = result["is_win"]
            amount_used  = result["amount"]
            payout       = result["payout"]
            raw_profit   = payout - amount_used if is_win else -amount_used
            bet_state    = "win" if is_win else "loss"
            rd           = result["result_display"]

            with self.lock:
                self.consecutive_errors = 0
                self.backoff_delay = 1.0
                self.total_bets += 1
                self.wagered    += amount_used
                self.profit     += raw_profit

                # Use afterBalance from API if available for accuracy
                after_bal = result.get("after_balance")
                if after_bal is not None:
                    self.current_balance = after_bal
                else:
                    self.current_balance += raw_profit

                if is_win:
                    self.wins += 1
                    if raw_profit > self.highest_win:
                        self.highest_win = raw_profit
                    self.current_streak = max(self.current_streak, 0) + 1
                    self.max_win_streak = max(self.max_win_streak, self.current_streak)
                else:
                    self.losses += 1
                    if amount_used > self.biggest_loss:
                        self.biggest_loss = amount_used
                    self.current_streak = min(self.current_streak, 0) - 1
                    self.max_loss_streak = max(self.max_loss_streak, abs(self.current_streak))

                if amount_used > self.highest_bet:
                    self.highest_bet = amount_used
                if self.current_balance > self.highest_balance:
                    self.highest_balance = self.current_balance
                if self.current_balance < self.lowest_balance:
                    self.lowest_balance = self.current_balance

                if self.total_bets % 5 == 0:
                    self.profit_history.append(self.profit)

                self.recent_bets.append({
                    "n": self.total_bets, "time": datetime.now().isoformat(),
                    "amt": amount_used, "result": rd,
                    "state": bet_state, "pnl": raw_profit, "bal": self.current_balance,
                })

                # BPS/BPM tracking
                now = time.time()
                elapsed = now - self.session_start
                if elapsed > 0:
                    self.bets_per_second = self.total_bets / elapsed
                    self.bets_per_minute = self._bets_this_min

                sec_key = int(now)
                min_key = int(now) // 60
                if sec_key != self._current_sec:
                    if self._current_sec > 0 and self._bets_this_sec > 0:
                        if self._bets_this_sec > self.peak_bps:
                            self.peak_bps = self._bets_this_sec
                        if self._bets_this_sec < self.low_bps:
                            self.low_bps = self._bets_this_sec
                    self._current_sec = sec_key
                    self._bets_this_sec = 1
                else:
                    self._bets_this_sec += 1
                if min_key != self._current_min:
                    if self._current_min > 0 and self._bets_this_min > 0:
                        if self._bets_this_min > self.peak_bpm:
                            self.peak_bpm = self._bets_this_min
                        if self._bets_this_min < self.low_bpm:
                            self.low_bpm = self._bets_this_min
                    self._current_min = min_key
                    self._bets_this_min = 1
                else:
                    self._bets_this_min += 1

                sign  = "+" if raw_profit >= 0 else ""
                emoji = "W" if is_win else "L"
                self.status = f"{emoji} {'WIN' if is_win else 'LOSS'} | {rd} | P/L: {sign}{raw_profit:.8f}"
                self.last_error = ""

            self._queue_bet(result, raw_profit, self.current_balance)
            self._periodic_save()

            # profit-based base bet increment
            if (self.profit_increment and self.profit_threshold
                    and self.profit >= self.next_profit_milestone):
                self.base_bet += self.profit_increment
                self.next_profit_milestone += self.profit_threshold
                logger.info("User %d: PROFIT INCREMENT base_bet -> %.8f (next at %.8f profit)",
                            self.user_id, self.base_bet, self.next_profit_milestone)

            # rule-based strategy
            if self.strategy_key == "7" and self.custom_rules:
                self._apply_rules(bet_state)

            raw_next = self._compute_next_bet(bet_state)
            self.current_bet = max(self.base_bet, raw_next)
            last_result = bet_state

            # milestone callbacks
            if self.on_milestone:
                reason = self._check_milestone(bet_state)
                if reason:
                    try:
                        s = self.get_status()
                        s["milestone_reason"] = reason
                        self.on_milestone(s)
                        logger.info("User %d: Milestone fired: %s", self.user_id, reason)
                    except Exception as e:
                        logger.error("User %d: Milestone callback failed: %s", self.user_id, e)

        # session ended
        self._flush_bets()
        if self.session_id:
            self._db_save_session(final=True)
        self._close_conn()
        logger.info("User %d: Session #%d ended — %s",
                     self.user_id, self.session_id, self.stop_reason or "stopped")
        if self.on_stop:
            try:
                self.on_stop(self.stop_reason or "Stopped by user")
            except Exception:
                pass
