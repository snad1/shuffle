"""BettingEngine — per-user betting instance running as async task.

Key features:
  - Fully async (no threads) — single event-loop, asyncio.create_task
  - Batched DB writes (flush every 50 bets + save session stats on each flush)
  - _db_save_session(final=False) for periodic saves without setting ended_at
  - Multi-game support via game registry (Limbo + Dice)
  - Bearer token auth + curl_cffi async for Cloudflare TLS bypass
  - Profit-based base bet increment
  - Milestone callbacks
  - Production-safe error messages via _set_error()
"""

import asyncio
import json
import os
import time
import random
import string
import sqlite3
import threading
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional

from .config import API_URL, APP_ENV, MIN_BET, logger
from core.database import init_db, db_connect, cleanup_old_bets, cleanup_live_bets
from core.strategy import StrategyRule, load_rules_from_text
from core.engine import compute_next_bet, apply_action, evaluate_rules
from shared.core.session_rules import (
    STREAK_RESET_REASON, record_outcome, streak_reset_triggered, streak_reset_stop_reason,
)
from shared.core.run_increment import (
    check_profit_increment, db_max_session_id, increment_on, load_run, next_milestone,
    parse_scope, run_is_stale, run_profit_done, save_run,
)

# One run file per user, shared by every slot that user runs. The lock guards
# the read-modify-write of the bump counter so two slots cannot drop each
# other's bump. All engines live in one process, so a thread lock is enough.
_RUN_LOCKS: Dict[int, threading.Lock] = {}


def _run_lock(user_id: int) -> threading.Lock:
    lock = _RUN_LOCKS.get(user_id)
    if lock is None:
        lock = _RUN_LOCKS[user_id] = threading.Lock()
    return lock

# Try curl_cffi async for TLS fingerprint bypass; fall back to httpx
try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession
    _HAS_CFFI_ASYNC = True
except ImportError:
    _HAS_CFFI_ASYNC = False

import httpx

# ── Batching / periodic save constants ──────────────────
BET_BATCH_SIZE     = 50      # flush bets + session stats to DB every N bets
SESSION_SAVE_SECS  = 30      # fallback: save session stats every N seconds
REQUEST_TIMEOUT    = httpx.Timeout(15.0, connect=5.0)
MAX_TIMEOUT_STREAK = 3       # recreate HTTP session after this many consecutive timeouts


def _gen_window_id() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=21))


# ── GraphQL queries / mutations ──────────────────────────
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
          userDiceDirection
          userValue
          resultValue
          resultRaw
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
            "data": {
                "amount": f"{engine.current_bet:.8f}",
                "bet": f"{engine.multiplier_target:.2f}",
                "currency": engine.currency.upper(),
                "windowId": _gen_window_id(),
                "usdAmount": 0,
            }
        },
    }


def _limbo_parse(data, engine=None):
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
    }


def _dice_build(engine):
    return {
        "operationName": "DicePlay",
        "query": _DICE_QUERY,
        "variables": {
            "data": {
                "amount": f"{engine.current_bet:.8f}",
                "bet": f"{engine.dice_target:.2f}",
                "currency": engine.currency.upper(),
                "direction": engine.dice_condition.upper(),
                "usdAmount": 0,
            }
        },
    }


def _dice_parse(data, engine=None):
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
    }


GAMES = {
    "limbo": {
        "label": "Limbo",
        "response_key": "limboPlay",
        "build_payload": _limbo_build,
        "parse_result": _limbo_parse,
    },
    "dice": {
        "label": "Dice",
        "response_key": "dicePlay",
        "build_payload": _dice_build,
        "parse_result": _dice_parse,
    },
}


def _cfg(config: dict, key: str, default, typ=float):
    """Get config value; treat None as default but allow 0."""
    v = config.get(key)
    return typ(v) if v is not None else typ(default)


class BettingEngine:
    """Self-contained betting engine for one user. Runs as an async task."""

    def __init__(self, user_id: int, db_path: str, config: dict):
        self.user_id = user_id
        self.db_path = db_path

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
        self._last_bet_start_time: float = 0.0  # monotonic ts for bet_delay pacing
        # ── streak delay: pause after N consecutive wins/losses ──
        self.streak_delay_loss = self._parse_streak_delay(config.get("streak_delay_loss"))
        self.streak_delay_win  = self._parse_streak_delay(config.get("streak_delay_win"))
        # streak_delay_bets "N:seconds" — every N total bets, delay X seconds (regardless of result)
        self.streak_delay_bets = self._parse_streak_delay(config.get("streak_delay_bets"))
        # ── adaptive base bet ──
        # basebet_pct: base_bet = balance * pct, recomputed on session start + profit-increment
        self.basebet_pct       = float(config.get("basebet_pct") or 0)
        # streakbet_loss "N:multiplier" — after every N consecutive losses, current_bet *= multiplier
        self.streakbet_loss    = self._parse_streak_delay(config.get("streakbet_loss"))
        self.proxy             = config.get("proxy") or None
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
        # Streak reset (WolfBet v2.32.0): stop on the first win after this many
        # consecutive losses (profit >= 0 only); handlers restart after
        # streak_reset_delay. The delay is kept here for /status only.
        self.streak_reset_losses = config.get("streak_reset_losses")
        self.streak_reset_delay  = config.get("streak_reset_delay")

        # ── session state ──
        self.running         = False
        self.paused          = False
        self.session_id      = None
        self.session_start   = 0.0
        self.session_bet_ended_at: Optional[float] = None
        self.session_pause_started_at: Optional[float] = None
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
        # Length of the loss streak the latest win just broke (0 if none).
        self.last_broken_loss_streak = 0
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
        # Sliding 60s window — what the bot is doing RIGHT NOW (vs. session average above)
        self.recent_bps      = 0.0
        self.recent_bpm      = 0.0
        self._recent_bet_times: deque = deque(maxlen=2000)
        # Per-cycle overhead (engine + event-loop scheduler), exp-smoothed ms
        # Answers: "is something blocking the bot or is this normal Python overhead?"
        self._cycle_overhead_ms: float = 0.0
        self._iter_start_mono: float = 0.0  # set at top of loop iteration
        # Full per-bet cycle time (T_start_N - T_start_N-1), exp-smoothed ms.
        # This is the bps reciprocal — Cycle*bps ≈ 1000. Honest about scheduler jitter.
        self._cycle_ms: float = 0.0
        # get_status() cache — milestone notifier + /status often called within ms
        # of each other; cache the dict briefly to avoid rebuilding 50 fields twice
        self._status_cache: Optional[dict] = None
        self._status_cache_ts: float = 0.0
        self.profit_history  = deque(maxlen=40)
        self.profit_history.append(0.0)
        self.recent_bets     = deque(maxlen=5)
        self._chart_snapshots: list = []    # [[bet_num, profit, balance], ...]
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
        self.peak_api_ms           = 0.0  # max single-bet API response time this session
        self._api_samples: deque   = deque(maxlen=200)  # ring buffer for p50/median calc
        self._on_target_count: int = 0  # bets where api_ms ≤ bet_delay (the happy path)

        # ── strategy internals ──
        self.dalembert_unit  = 0
        self.paroli_count    = 0

        # ── profit-based base bet increment ──
        self.profit_increment     = config.get("profit_increment")
        self.profit_threshold     = config.get("profit_threshold")
        self.next_profit_milestone = float(self.profit_threshold) if self.profit_threshold else 0.0

        # ── run-wide increment ──
        # scope "run": the user's total across every session (all slots) since
        # the run started; bumps are shared through <user dir>/run.json.
        self.profit_increment_scope = self._parse_scope(config.get("profit_increment_scope"))
        _origin = config.get("_run_base_origin")
        self._run_base_origin: Optional[float] = float(_origin) if _origin is not None else None
        self.run_first_session: Optional[int] = None
        self.run_bumps = 0
        self.run_since: Optional[str] = None
        self._run_others = 0.0     # DB profit of the run's other sessions (cached)
        self._run_bump_pending = False   # another slot's bump applied by _run_sync

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
        self._last_cleanup = 0.0
        self.purge_days = int(config.get("purge_days") or 1)
        self._db_connection: Optional[sqlite3.Connection] = None

        # ── async HTTP client ──
        self._http = None

        # ── async task ──
        self._task: Optional[asyncio.Task] = None

    # ── HTTP ─────────────────────────────────────────────
    async def _recreate_http(self):
        """Close and recreate the async HTTP client."""
        try:
            if self._http:
                if hasattr(self._http, 'aclose'):
                    await self._http.aclose()
                else:
                    self._http.close()
        except Exception:
            pass
        if _HAS_CFFI_ASYNC:
            kwargs = {"impersonate": "chrome"}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._http = CffiAsyncSession(**kwargs)
        else:
            self._http = httpx.AsyncClient(
                headers=self._headers(), timeout=REQUEST_TIMEOUT,
                proxy=self.proxy if self.proxy else None,
            )
        logger.info("User %d: HTTP client recreated", self.user_id)

    @staticmethod
    def _parse_streak_delay(value) -> tuple:
        """Parse 'N:seconds' streak delay config. Returns (streak_count, delay_seconds) or (0, 0)."""
        if not value:
            return (0, 0.0)
        try:
            parts = str(value).split(":")
            return (int(parts[0]), float(parts[1]))
        except (IndexError, ValueError):
            return (0, 0.0)

    def _apply_basebet_pct(self) -> bool:
        """If basebet_pct configured, recompute base_bet from current balance. Returns True if changed."""
        if self.basebet_pct <= 0 or self.current_balance <= 0:
            return False
        new_base = round(self.current_balance * self.basebet_pct, 8)
        if new_base <= 0:
            return False
        if abs(new_base - self.base_bet) < 1e-12:
            return False
        self.base_bet = new_base
        self.current_bet = new_base
        return True

    def _get_streakbet_loss_factor(self) -> float:
        """Return current_bet multiplier when on a qualifying loss streak. 1.0 = no change."""
        streak = self.current_streak
        count, mult = self.streakbet_loss
        if count > 0 and streak < 0 and abs(streak) > 0 and abs(streak) % count == 0:
            return mult
        return 1.0

    def _get_streak_delay(self) -> float:
        """Check if current streak/total-bets triggers a delay. Returns max applicable delay or 0."""
        streak = self.current_streak
        delay = 0.0
        if streak < 0 and self.streak_delay_loss[0] > 0:
            count, d = self.streak_delay_loss
            if abs(streak) > 0 and abs(streak) % count == 0:
                delay = max(delay, d)
        if streak > 0 and self.streak_delay_win[0] > 0:
            count, d = self.streak_delay_win
            if streak % count == 0:
                delay = max(delay, d)
        if self.streak_delay_bets[0] > 0 and self.total_bets > 0:
            count, d = self.streak_delay_bets
            if self.total_bets % count == 0:
                delay = max(delay, d)
        return delay

    def _set_error(self, friendly: str, technical: str = ""):
        """Set last_error: friendly message in production, technical details in dev."""
        if APP_ENV != "production" and technical:
            self.last_error = technical
        else:
            self.last_error = friendly
        if technical:
            logger.warning("User %d: %s", self.user_id, technical)

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

    async def _gql_post(self, payload: dict) -> Optional[dict]:
        if not self._http:
            await self._recreate_http()
        for attempt in range(2):
            try:
                r = await self._http.post(API_URL, headers=self._headers(), json=payload)
                break
            except (ConnectionError, ConnectionResetError, OSError) as e:
                if attempt == 0:
                    logger.warning("User %d: Connection reset — recreating HTTP client", self.user_id)
                    await self._recreate_http()
                    continue
                raise ConnectionError(f"Connection failed: {e}")
        if r.status_code != 200:
            body = r.text[:300] if r.text else "(empty)"
            raise ConnectionError(f"HTTP {r.status_code}: {body}")
        return r.json()

    # ── API ──────────────────────────────────────────────
    async def api_get_balances(self) -> list:
        """Fetch all balances via GraphQL."""
        payload = {
            "operationName": "GetMyProfile",
            "query": _BALANCE_QUERY,
            "variables": {},
        }
        try:
            data = await self._gql_post(payload)
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

    async def _api_test_connection(self) -> bool:
        """Test auth by fetching the user profile (read-only, no bet placed)."""
        try:
            balances = await self.api_get_balances()
            if isinstance(balances, list):
                return True
            self._set_error("Unexpected response from profile query")
            return False
        except Exception as e:
            self._set_error("Connection test failed", str(e)[:200])
            return False

    async def _api_place_bet(self, amount: float) -> Optional[dict]:
        """Place a bet. Returns parsed result dict or None."""
        if 0 < amount < MIN_BET:
            amount = MIN_BET
        if self.current_balance > 0 and amount > self.current_balance:
            self._set_error("Insufficient balance", f"bet {amount:.8f} > balance {self.current_balance:.8f}")
            self._insufficient_balance = True
            return None

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
            data = await self._gql_post(payload)
            self._last_api_ms = (time.time() - t0) * 1000
            if self._last_api_ms > self.peak_api_ms:
                self.peak_api_ms = self._last_api_ms
            self._api_samples.append(self._last_api_ms)
            if self.bet_delay > 0 and self._last_api_ms <= self.bet_delay * 1000:
                self._on_target_count += 1
            self._consecutive_timeouts = 0

            if data is None:
                self._set_error("Empty response")
                return None

            errors = data.get("errors")
            if errors:
                err_str = str(errors).lower()
                if "insufficient" in err_str:
                    self._set_error("Insufficient balance", f"Insufficient: {str(errors)[:120]}")
                    self._insufficient_balance = True
                    return None
                self._set_error("API error", f"API error: {str(errors)[:120]}")
                return None

            gql_data = data.get("data", {})
            raw = gql_data.get(resp_key)
            if not raw:
                self._set_error(f"Empty {resp_key}")
                return None

            self._api_ms_total += self._last_api_ms
            self._api_ms_count += 1
            return parse(raw, self)
        except ConnectionError as e:
            err = str(e)
            if "429" in err:
                self._set_error("Rate limited", "Rate limit 429")
            elif "insufficient" in err.lower():
                self._set_error("Insufficient balance", f"Balance error: {err[:120]}")
                self._insufficient_balance = True
            else:
                self._set_error("Connection error", err[:120])
        except (httpx.TimeoutException, httpx.ConnectError, OSError):
            self._consecutive_timeouts += 1
            self._set_error("Request timeout", f"Request timeout (#{self._consecutive_timeouts})")
            if self._consecutive_timeouts >= MAX_TIMEOUT_STREAK:
                logger.warning("User %d: %d consecutive timeouts — recreating HTTP client",
                               self.user_id, self._consecutive_timeouts)
                await self._recreate_http()
                self._consecutive_timeouts = 0
        except Exception as e:
            self._set_error("Bet request failed", str(e)[:120])
        return None

    # ── DB ───────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        """Open with check_same_thread=False directly (not via shared db_connect)
        so the engine survives a stale casino-shared install. Reused across
        asyncio.to_thread() calls; the engine owns it."""
        if self._db_connection is None:
            self._db_connection = sqlite3.connect(
                self.db_path, timeout=5, check_same_thread=False)
            self._db_connection.execute("PRAGMA journal_mode=WAL")
            # v1.7.0 — mirrors wolfbet v2.27.0. WAL-safe.
            self._db_connection.execute("PRAGMA synchronous = NORMAL")
            self._db_connection.execute("PRAGMA busy_timeout = 5000")
            self._db_connection.execute("PRAGMA cache_size = -32000")
            self._db_connection.execute("PRAGMA temp_store = MEMORY")
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
            "basebet_pct": self.basebet_pct or None,
            "streak_delay_loss": f"{self.streak_delay_loss[0]}:{self.streak_delay_loss[1]}" if self.streak_delay_loss[0] > 0 else None,
            "streak_delay_win": f"{self.streak_delay_win[0]}:{self.streak_delay_win[1]}" if self.streak_delay_win[0] > 0 else None,
            "streak_delay_bets": f"{self.streak_delay_bets[0]}:{self.streak_delay_bets[1]}" if self.streak_delay_bets[0] > 0 else None,
            "streakbet_loss": f"{self.streakbet_loss[0]}:{self.streakbet_loss[1]}" if self.streakbet_loss[0] > 0 else None,
            "milestone_bets": self.milestone_bets,
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

    # ── RUN-WIDE PROFIT INCREMENT ─────────────────────────
    # Scope "run": the user's total across every session (all slots) since the
    # run started; bumps are shared through <user dir>/run.json so they survive
    # stops, restarts and recurring cycles.
    @staticmethod
    def _parse_scope(value) -> str:
        """'Run ' -> 'run'; anything but session / run falls back to session."""
        try:
            return parse_scope(value)
        except ValueError:
            return "session"

    def _increment_on(self) -> bool:
        return increment_on(self.profit_increment, self.profit_threshold)

    def _run_path(self) -> str:
        return os.path.join(os.path.dirname(self.db_path) or ".", "run.json")

    def _save_run(self) -> None:
        """Persist the run, keeping the highest bump count any slot wrote."""
        if self.run_first_session is None:
            return
        with _run_lock(self.user_id):
            saved = load_run(self._run_path()) or {}
            self.run_bumps = max(self.run_bumps, int(saved.get("bumps") or 0))
            try:
                save_run(self._run_path(), {"first_session": self.run_first_session,
                                            "started_at": self.run_since,
                                            "bumps": self.run_bumps})
            except OSError as e:
                logger.warning("User %d: run file save failed: %s", self.user_id, e)

    def _end_run(self, reason: str) -> None:
        had = self.run_first_session is not None
        try:
            os.remove(self._run_path())
            had = True
        except OSError:
            pass
        self.run_first_session = None
        self.run_since = None
        self.run_bumps = 0
        self._run_others = 0.0
        if had:
            logger.info("User %d: RUN ended (%s) - the next run starts fresh",
                        self.user_id, reason)

    def _run_db_sum(self, first_sid: int) -> float:
        """Profit of the run's sessions (all slots) except this engine's own."""
        try:
            conn = db_connect(self.db_path)
        except sqlite3.Error as e:
            logger.warning("User %d: run profit query failed: %s", self.user_id, e)
            return 0.0
        try:
            return run_profit_done(conn, first_sid, exclude_sid=self.session_id or -1)
        finally:
            conn.close()

    def _db_max_session_id(self) -> int:
        try:
            conn = db_connect(self.db_path)
        except sqlite3.Error:
            return 0
        try:
            return db_max_session_id(conn)
        finally:
            conn.close()

    def run_profit_total(self) -> float:
        return self._run_others + self.profit

    def _apply_run_bumps(self) -> None:
        """base_bet = origin + bumps x increment (basebet_pct keeps its own path)."""
        if self.basebet_pct > 0 or self._run_base_origin is None or not self.profit_increment:
            return
        self.base_bet = round(
            self._run_base_origin + self.run_bumps * float(self.profit_increment), 8)
        self.current_bet = max(self.current_bet, self.base_bet) if self.total_bets else self.base_bet

    def _init_run(self, resumed: bool = False) -> None:
        """Join (or prepare) the user's run before betting. Scope "run" only.

        Off (none / 0) or per-session scope removes a leftover run file so old
        bumps never come back. A file pointing past the DB's last session (the
        stats were reset) starts a new run.
        """
        self.run_first_session = None
        self.run_since = None
        self.run_bumps = 0
        self._run_others = 0.0
        if self.profit_increment_scope != "run" or not self._increment_on():
            self._end_run("increment off" if self.profit_increment_scope == "run"
                          else "scope session")
            return
        if self._run_base_origin is None:
            # Fresh /bet: the config base is the origin. Resume: the snapshot
            # base already holds the bumps, so they are subtracted back out.
            self._run_base_origin = self.base_bet
            pending_resume_fix = resumed
        else:
            pending_resume_fix = False
        run = load_run(self._run_path())
        if run and run_is_stale(run, self._db_max_session_id()):
            logger.warning("User %d: RUN file points past the stats DB - starting a new run",
                           self.user_id)
            run = None
        if run:
            self.run_first_session = run["first_session"]
            self.run_since = run["started_at"]
            self.run_bumps = run["bumps"]
            self._run_others = self._run_db_sum(run["first_session"])
            if pending_resume_fix and self.profit_increment:
                self._run_base_origin = round(
                    self.base_bet - self.run_bumps * float(self.profit_increment), 8)
            self._apply_run_bumps()
        if self.profit_threshold:
            self.next_profit_milestone = next_milestone(self.run_bumps,
                                                        float(self.profit_threshold))
        logger.info("User %d: RUN %s since #%s bumps=%d run profit=%+.8f base_bet=%.8f",
                    self.user_id, "joined" if run else "new",
                    self.run_first_session or "next", self.run_bumps,
                    self._run_others, self.base_bet)

    def _attach_run(self, session_id: int) -> None:
        if (self.profit_increment_scope == "run" and self._increment_on()
                and self.run_first_session is None and session_id):
            run = load_run(self._run_path())     # another slot may have started one
            if run:
                self.run_first_session = run["first_session"]
                self.run_since = run["started_at"]
                self.run_bumps = run["bumps"]
                self._apply_run_bumps()
                return
            self.run_first_session = session_id
            self.run_since = datetime.now().isoformat()
            self.run_bumps = 0
            self._save_run()

    def _run_sync(self) -> bool:
        """Refresh the other slots' profit and shared bumps; True if base_bet changed."""
        if self.run_first_session is None:
            return False
        run = load_run(self._run_path())
        if not run or run["first_session"] != self.run_first_session:
            # /runreset or another slot ended the run.
            self.run_first_session = None
            self.run_since = None
            self.run_bumps = 0
            self._run_others = 0.0
            return False
        self._run_others = self._run_db_sum(self.run_first_session)
        if run["bumps"] != self.run_bumps:
            self.run_bumps = run["bumps"]
            if self.profit_threshold:
                self.next_profit_milestone = next_milestone(
                    self.run_bumps, float(self.profit_threshold))
            before = self.base_bet
            self._apply_run_bumps()
            if self.base_bet != before:
                self._run_bump_pending = True
                return True
        return False

    def _maybe_profit_increment(self) -> bool:
        """Bump base_bet at the next milestone. True when base_bet changed.

        Scope "session": this session's profit. Scope "run": the user's run
        total; the bump is shared through run.json, so every slot follows.
        """
        if self._run_bump_pending:
            self._run_bump_pending = False      # a bump made by another slot
            return True
        if not self._increment_on():
            return False
        run = self.profit_increment_scope == "run" and self.run_first_session is not None
        if run:
            cur = load_run(self._run_path())
            if (cur and cur["first_session"] == self.run_first_session
                    and cur["bumps"] > self.run_bumps):
                self.run_bumps = cur["bumps"]
                if self.profit_threshold:
                    self.next_profit_milestone = next_milestone(
                        self.run_bumps, float(self.profit_threshold))
            profit = self.run_profit_total()
        else:
            profit = self.profit
        bumped, base, next_ms, bumps = check_profit_increment(
            profit, self.next_profit_milestone, self.profit_threshold,
            self.profit_increment, self.base_bet, self.run_bumps if run else 0)
        if not bumped:
            return False
        if self.basebet_pct > 0 and self.current_balance > 0:
            self.base_bet = round(self.current_balance * self.basebet_pct, 8)
            if run:
                self.run_bumps = bumps
        elif run:
            self.run_bumps = bumps
            self._apply_run_bumps()
        else:
            self.base_bet = base
        self.next_profit_milestone = next_ms
        if run:
            self._save_run()
        logger.info("User %d: %sPROFIT INCREMENT base_bet -> %.8f (next at %.8f profit)",
                    self.user_id, "RUN " if run else "", self.base_bet,
                    self.next_profit_milestone)
        return True


    def _queue_bet(self, result: dict, profit: float, balance: float):
        """Queue a bet row for batched insertion.

        Flush is intentionally NOT triggered here even when the queue passes
        BET_BATCH_SIZE — _periodic_save (called from the async loop) handles
        flushing via asyncio.to_thread so DB I/O never blocks the event loop.
        Queue growing past 50 between saves is harmless (~6 KB per 50 rows;
        even 1000 queued ≈ 128 KB).
        """
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
                json.dumps(self._chart_snapshots[-2000:]),
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
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?,
                        chart_snapshots=?
                    WHERE id=?
                """, (
                    # v1.7.0 — freeze ended_at during pauses.
                    (datetime.fromtimestamp(self.session_bet_ended_at).isoformat()
                     if self.session_bet_ended_at else datetime.now().isoformat()),
                ) + fields)
            else:
                conn.execute("""
                    UPDATE sessions SET
                        total_bets=?, wins=?, losses=?,
                        profit=?, wagered=?, end_balance=?,
                        max_win_streak=?, max_loss_streak=?,
                        highest_balance=?, lowest_balance=?,
                        highest_win=?, biggest_loss=?,
                        bets_per_minute=?, bets_per_second=?,
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?,
                        chart_snapshots=?
                    WHERE id=?
                """, fields)
            conn.commit()
            self._last_session_save = time.time()
        except Exception as e:
            logger.error("User %d: DB save_session failed: %s", self.user_id, e)

    async def _periodic_save(self):
        """Flush bets and save session stats periodically.

        DB calls run in a worker thread so the event loop stays free for other
        engines to keep betting — without this, 6 concurrent sessions
        serialize on each other's flushes and bps collapses to ~1/N.
        """
        now = time.time()
        if now - self._last_session_save >= SESSION_SAVE_SECS:
            if self._bet_queue:
                await asyncio.to_thread(self._flush_bets)
            else:
                await asyncio.to_thread(self._db_save_session)
            # Refresh the run total (other slots) after each save.
            if self.run_first_session is not None:
                await asyncio.to_thread(self._run_sync)

        # Live cleanup: purge old bets every hour to prevent DB bloat
        if now - self._last_cleanup >= 3600:
            try:
                deleted = await asyncio.to_thread(
                    cleanup_live_bets, self.db_path, self.purge_days)
                if deleted > 0:
                    logger.info("User %d: Live cleanup: deleted %d old bets (>%dd)",
                                self.user_id, deleted, self.purge_days)
                self._last_cleanup = now
            except Exception as e:
                logger.warning("User %d: Live cleanup failed: %s", self.user_id, e)

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
        # Streak reset — only on the win that broke an N+ loss streak, not in the red.
        if streak_reset_triggered(self.current_streak, self.last_broken_loss_streak,
                                  self.streak_reset_losses, p):
            return True, streak_reset_stop_reason(self.last_broken_loss_streak)
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
    async def start(self) -> bool:
        """Connect, fetch balance, start betting task. Returns True on success."""
        if not self._http:
            await self._recreate_http()

        if not await self._api_test_connection():
            return False

        try:
            balances = await self.api_get_balances()
        except Exception as e:
            self._set_error("Balance fetch failed", f"Balance fetch failed: {e}")
            return False

        bal = 0.0
        for b in balances:
            if b.get("currency", "").lower() == self.currency.lower():
                bal = float(b.get("amount", 0))
                break
        if bal <= 0 and self.base_bet > 0:
            self._set_error(f"No {self.currency.upper()} balance found")
            return False

        init_db(self.db_path)
        cleanup_old_bets(self.db_path)
        self.start_balance   = bal
        self.current_balance = bal
        self.highest_balance = bal
        self.lowest_balance  = bal
        if self._apply_basebet_pct():
            logger.info("User %d: basebet_pct=%.6f -> base_bet=%.8f (balance=%.8f)",
                        self.user_id, self.basebet_pct, self.base_bet, bal)
        self.session_start   = time.time()
        self._last_session_save = time.time()
        self._init_run()
        self.session_id      = self._db_start_session()
        self._attach_run(self.session_id)
        self.running         = True
        self.status          = "Starting..."

        self._task = asyncio.create_task(self._betting_loop())
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
            "streak_reset_losses": self.streak_reset_losses,
            "streak_reset_delay": self.streak_reset_delay,
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "profit_increment_scope": self.profit_increment_scope,
            "_run_base_origin": self._run_base_origin,
            "proxy": self.proxy,
            "milestone_bets": self.milestone_bets,
            "milestone_wins": self.milestone_wins,
            "milestone_losses": self.milestone_losses,
            "milestone_profit": self.milestone_profit,
            "streak_delay_loss": f"{self.streak_delay_loss[0]}:{self.streak_delay_loss[1]}" if self.streak_delay_loss[0] > 0 else None,
            "streak_delay_win":  f"{self.streak_delay_win[0]}:{self.streak_delay_win[1]}" if self.streak_delay_win[0] > 0 else None,
            "streak_delay_bets": f"{self.streak_delay_bets[0]}:{self.streak_delay_bets[1]}" if self.streak_delay_bets[0] > 0 else None,
            "streakbet_loss":    f"{self.streakbet_loss[0]}:{self.streakbet_loss[1]}" if self.streakbet_loss[0] > 0 else None,
            "basebet_pct":       self.basebet_pct or None,
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
            "last_broken_loss_streak": self.last_broken_loss_streak,
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
        self.last_broken_loss_streak = snap.get("last_broken_loss_streak", 0)
        self.highest_bet       = snap["highest_bet"]
        self.highest_win       = snap["highest_win"]
        self.biggest_loss      = snap["biggest_loss"]
        self.highest_balance   = snap["highest_balance"]
        self.lowest_balance    = snap.get("lowest_balance", float("inf"))
        self.dalembert_unit    = snap.get("dalembert_unit", 0)
        self.paroli_count      = snap.get("paroli_count", 0)
        self.next_profit_milestone  = snap.get("next_profit_milestone", 0.0)
        self._last_profit_milestone = snap.get("_last_profit_milestone", 0.0)
        self._run_base_origin       = snap["config"].get("_run_base_origin")

    async def start_resumed(self) -> bool:
        if not self._http:
            await self._recreate_http()
        init_db(self.db_path)

        if not await self._api_test_connection():
            self._set_error("Resume connection failed", f"Resume failed: {self.last_error}")
            return False

        self.running = True
        self.paused = False
        self.status = "Resumed"
        self._last_session_save = time.time()
        self._init_run(resumed=True)
        self._attach_run(self.session_id)
        elapsed = time.time() - self.session_start if self.session_start else 0
        if elapsed > 0 and self.total_bets > 0:
            self.bets_per_second = self.total_bets / elapsed
            self.bets_per_minute = self.bets_per_second * 60
        self._task = asyncio.create_task(self._betting_loop())
        logger.info("User %d: Resumed session #%d", self.user_id, self.session_id)
        return True

    def stop(self):
        self.running = False

    async def mutate(self, changes: dict) -> str:
        """Apply config changes to a running session. Returns status message."""
        msgs = []
        if "bet_delay" in changes:
            self.bet_delay = float(changes["bet_delay"])
            msgs.append(f"Delay: {self.bet_delay}s")
        if "streak_delay_loss" in changes:
            self.streak_delay_loss = self._parse_streak_delay(changes["streak_delay_loss"])
            msgs.append(f"Streak delay loss: {self.streak_delay_loss[0]}x → {self.streak_delay_loss[1]:.3f}s")
        if "streak_delay_win" in changes:
            self.streak_delay_win = self._parse_streak_delay(changes["streak_delay_win"])
            msgs.append(f"Streak delay win: {self.streak_delay_win[0]}x → {self.streak_delay_win[1]:.3f}s")
        if "streak_delay_bets" in changes:
            self.streak_delay_bets = self._parse_streak_delay(changes["streak_delay_bets"])
            if self.streak_delay_bets[0] > 0:
                msgs.append(f"Streak delay bets: every {self.streak_delay_bets[0]} bets → {self.streak_delay_bets[1]:.3f}s")
            else:
                msgs.append("Streak delay bets: off")
        if "streakbet_loss" in changes:
            self.streakbet_loss = self._parse_streak_delay(changes["streakbet_loss"])
            if self.streakbet_loss[0] > 0:
                msgs.append(f"Streak bet loss: every {self.streakbet_loss[0]} losses → x{self.streakbet_loss[1]:.3f}")
            else:
                msgs.append("Streak bet loss: off")
        if "basebet_pct" in changes:
            v = changes["basebet_pct"]
            self.basebet_pct = float(v) if v is not None else 0.0
            if self.basebet_pct > 0 and self.current_balance > 0:
                self._apply_basebet_pct()
                msgs.append(f"Basebet pct: {self.basebet_pct*100:.4f}% → base={self.base_bet:.8f}")
            else:
                msgs.append("Basebet pct: off")
        if "max_profit" in changes:
            v = changes["max_profit"]
            self.max_profit = float(v) if v is not None else None
            msgs.append(f"Max profit: {float(v):.8f}" if v is not None else "Max profit: off")
        if "max_loss" in changes:
            v = changes["max_loss"]
            self.max_loss = float(v) if v is not None else None
            msgs.append(f"Max loss: {float(v):.8f}" if v is not None else "Max loss: off")
        if "max_bets" in changes:
            v = changes["max_bets"]
            self.max_bets = int(v) if v is not None else None
            msgs.append(f"Max bets: {v}" if v is not None else "Max bets: off")
        if "max_wins" in changes:
            v = changes["max_wins"]
            self.max_wins = int(v) if v is not None else None
            msgs.append(f"Max wins: {v}" if v is not None else "Max wins: off")
        if "stop_on_balance" in changes:
            v = changes["stop_on_balance"]
            self.stop_on_balance = float(v) if v is not None else None
            msgs.append(f"Min balance: {float(v):.8f}" if v is not None else "Min balance: off")
        if "streak_reset_losses" in changes:
            v = changes["streak_reset_losses"]
            self.streak_reset_losses = int(v) if v is not None else None
            msgs.append(f"Streak reset: after {v} losses" if v is not None else "Streak reset: off")
        if "milestone_bets" in changes:
            self.milestone_bets = int(changes["milestone_bets"])
            msgs.append(f"Milestone bets: {self.milestone_bets}")
        if "milestone_wins" in changes:
            self.milestone_wins = int(changes["milestone_wins"])
            msgs.append(f"Milestone wins: {self.milestone_wins}")
        if "profit_increment" in changes:
            self.profit_increment = changes["profit_increment"]
            msgs.append(f"Profit increment: {float(self.profit_increment):.8f}" if self.profit_increment else "Profit increment: off")
        if "profit_threshold" in changes:
            self.profit_threshold = changes["profit_threshold"]
            if self.profit_threshold:
                done = self.run_bumps if self.profit_increment_scope == "run" else 0
                self.next_profit_milestone = next_milestone(done, self.profit_threshold)
            msgs.append(f"Profit threshold: {float(self.profit_threshold):.8f}" if self.profit_threshold else "Profit threshold: off")
        if "profit_increment_scope" in changes:
            self.profit_increment_scope = parse_scope(changes["profit_increment_scope"])
            msgs.append(f"Profit increment scope: {self.profit_increment_scope} "
                        "(applies on the next session)")
        # A run ends when the increment is turned off and starts fresh when it
        # is turned back on, so a stale run never outlives the setting.
        if (self.profit_increment_scope == "run"
                and {"profit_increment", "profit_threshold"} & set(changes)):
            if not self._increment_on():
                self._end_run("increment off")
                msgs.append("Run total ended (increment off)")
            elif self.run_first_session is None and self.session_id:
                self._run_base_origin = self.base_bet
                self._attach_run(self.session_id)
                if self.profit_threshold:
                    self.next_profit_milestone = float(self.profit_threshold)
                msgs.append(f"Run total started at session #{self.session_id}")
        if "base_bet" in changes:
            self.base_bet = float(changes["base_bet"])
            # Don't reset current_bet during loss streak — applies on next win
            msgs.append(f"Base bet: {self.base_bet:.8f} (applies on next win)")
        if "multiplier" in changes:
            self.multiplier_target = float(changes["multiplier"])
            msgs.append(f"Multiplier: {self.multiplier_target:.2f}")
        if "loss_mult" in changes:
            self.loss_mult = float(changes["loss_mult"])
            msgs.append(f"Loss mult: {self.loss_mult:.2f}")
        if "win_mult" in changes:
            self.win_mult = float(changes["win_mult"])
            msgs.append(f"Win mult: {self.win_mult:.2f}")
        return "; ".join(msgs) if msgs else "No changes applied"

    def pause(self):
        # v1.7.0 — freeze uptime.
        if not self.paused:
            now = time.time()
            self.session_bet_ended_at = now
            self.session_pause_started_at = now
        self.paused = True

    def resume(self):
        self.session_bet_ended_at = None
        self.session_pause_started_at = None
        self.paused = False

    def get_status(self) -> dict:
        # Cache for ~250ms — milestone notifier and /status often fire within ms
        # of each other; avoid rebuilding the 50-field dict every time.
        now_mono = time.monotonic()
        if self._status_cache is not None and (now_mono - self._status_cache_ts) < 0.25:
            return self._status_cache
        # v1.7.0 — freeze uptime at bet-end during pauses.
        now = time.time()
        ref = self.session_bet_ended_at if self.session_bet_ended_at else now
        elapsed = ref - self.session_start if self.session_start else 0
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
            strat_detail = f"loss_mult: {self.loss_mult:.2f}x"
        elif self.strategy_key in ("3", "5"):
            strat_detail = f"win_mult: {self.win_mult:.2f}x"

        # rule descriptions
        rule_descs = []
        for r in self.custom_rules:
            d = r.to_dict()
            rule_descs.append(f"{d.get('trigger','')} {d.get('condition','')} {d.get('threshold','')}: {d.get('action','')} {d.get('value','')}")

        status = {
            "running": self.running,
            "paused": self.paused,
            "session_id": self.session_id,
            "uptime": f"{h}h {m}m {s}s",
            "uptime_sec": int(elapsed),
            "pause_remaining_sec": 0,
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
            "recent_bps": self.recent_bps,
            "recent_bpm": self.recent_bpm,
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
            "profit_increment_scope": self.profit_increment_scope,
            "run_profit_total": self.run_profit_total(),
            "run_first_session": self.run_first_session,
            "run_bumps": self.run_bumps,
            "next_profit_milestone": self.next_profit_milestone,
            "milestone_bets": self.milestone_bets,
            "streak_delay_loss": self.streak_delay_loss,
            "streak_delay_win": self.streak_delay_win,
            "streak_delay_bets": self.streak_delay_bets,
            "streakbet_loss": self.streakbet_loss,
            "basebet_pct": self.basebet_pct,
            "streak_reset_losses": self.streak_reset_losses,
            "streak_reset_delay": self.streak_reset_delay,
            "rule": getattr(self, "rule", None),
            "bet_value": getattr(self, "bet_value", None),
            "win_mult": self.win_mult,
            "loss_mult": self.loss_mult,
            "pipeline": getattr(self, "pipeline", 1),
            "game_label": game_info,
            "rules": rule_descs,
            "status": self.status,
            "last_error": self.last_error,
            "api_ms": self._last_api_ms,
            "api_avg_ms": (self._api_ms_total / self._api_ms_count) if self._api_ms_count > 0 else 0,
            "api_peak_ms": self.peak_api_ms,
            "overhead_ms": self._cycle_overhead_ms,
            "cycle_ms": self._cycle_ms,
        }
        # API median (p50) and on-target % (api ≤ bet_delay). Explains the
        # Efficiency ceiling: 95% on-target → ~95% efficiency is the upper bound.
        p50 = (sorted(self._api_samples)[len(self._api_samples) // 2]
               if self._api_samples else 0)
        on_target_pct = ((self._on_target_count / self._api_ms_count) * 100
                         if self._api_ms_count > 0 and self.bet_delay > 0 else None)
        status["api_p50_ms"] = p50
        status["on_target_pct"] = on_target_pct
        # Efficiency = recent bps / theoretical max bps (capped at 100%).
        # theoretical max = 1 / max(bet_delay, api_avg) — whichever bounds the cycle.
        api_avg_s = ((self._api_ms_total / self._api_ms_count) / 1000.0) if self._api_ms_count > 0 else 0
        cycle_floor = max(self.bet_delay, api_avg_s)
        theoretical_max_bps = (1.0 / cycle_floor) if cycle_floor > 0 else 0
        efficiency_pct = (self.recent_bps / theoretical_max_bps * 100) if theoretical_max_bps > 0 else 0
        status["theoretical_max_bps"] = theoretical_max_bps
        status["efficiency_pct"] = min(100.0, efficiency_pct)
        self._status_cache = status
        self._status_cache_ts = now_mono
        return status

    # ── BETTING LOOP ─────────────────────────────────────
    async def _betting_loop(self):
        last_result = "none"
        logger.info("User %d: Session #%d started (%s %s)",
                     self.user_id, self.session_id, self.game, self.strategy)

        while self.running:
            if self.paused:
                self.status = "PAUSED"
                await asyncio.sleep(0.5)
                continue

            stop, reason = self._should_stop()
            if stop:
                self.running = False
                self.stop_reason = reason
                self.status = f"STOPPED: {reason}"
                await asyncio.to_thread(self._flush_bets)
                await asyncio.to_thread(self._db_save_session, True)
                break

            # bet pacing — minimum interval since the previous bet started
            if self.bet_delay > 0:
                wait = (self._last_bet_start_time + self.bet_delay) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            prev_bet_start = self._last_bet_start_time
            self._last_bet_start_time = time.monotonic()
            self._iter_start_mono = self._last_bet_start_time
            # Full cycle = T_start_N - T_start_N-1 (= 1/bps). Skip first iter.
            if prev_bet_start > 0:
                self._cycle_ms = (0.9 * self._cycle_ms
                                  + 0.1 * (self._last_bet_start_time - prev_bet_start) * 1000)

            self.status = f"Placing bet #{self.total_bets + 1}..."
            result = await self._api_place_bet(self.current_bet)

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
                    await asyncio.to_thread(self._flush_bets)
                    await asyncio.to_thread(self._db_save_session, True)
                    break
                self.consecutive_errors += 1
                self.backoff_delay = min(self.backoff_delay * 2, 30.0)
                await asyncio.sleep(self.backoff_delay)
                continue

            is_win       = result["is_win"]
            amount_used  = result["amount"]
            payout       = result["payout"]
            raw_profit   = payout - amount_used if is_win else -amount_used
            bet_state    = "win" if is_win else "loss"
            rd           = result["result_display"]

            self.consecutive_errors = 0
            self.backoff_delay = 1.0
            self.total_bets += 1
            self.wagered    += amount_used
            self.profit     += raw_profit

            after_bal = result.get("after_balance")
            if after_bal is not None:
                self.current_balance = after_bal
            else:
                self.current_balance += raw_profit

            if is_win:
                self.wins += 1
                if raw_profit > self.highest_win:
                    self.highest_win = raw_profit
            else:
                self.losses += 1
                if amount_used > self.biggest_loss:
                    self.biggest_loss = amount_used
            self.current_streak, broken = record_outcome(self.current_streak, is_win)
            if broken is not None:
                self.last_broken_loss_streak = broken
            if is_win:
                self.max_win_streak = max(self.max_win_streak, self.current_streak)
            else:
                self.max_loss_streak = max(self.max_loss_streak, abs(self.current_streak))

            if amount_used > self.highest_bet:
                self.highest_bet = amount_used
            if self.current_balance > self.highest_balance:
                self.highest_balance = self.current_balance
            if self.current_balance < self.lowest_balance:
                self.lowest_balance = self.current_balance

            # Streak delay
            streak_pause = self._get_streak_delay()
            if streak_pause > 0:
                self.status = f"Streak delay {streak_pause:.3f}s (streak {self.current_streak})"
                await asyncio.sleep(streak_pause)

            if self.total_bets % 5 == 0:
                self.profit_history.append(self.profit)

            # Chart snapshot every 100 bets (survives bet cleanup)
            if self.total_bets % 100 == 0:
                self._chart_snapshots.append([self.total_bets, round(self.profit, 10), round(self.current_balance, 10)])

            self.recent_bets.append({
                "n": self.total_bets, "time": datetime.now().isoformat(),
                "amt": amount_used, "result": rd,
                "state": bet_state, "pnl": raw_profit, "bal": self.current_balance,
            })

            # BPS/BPM tracking — cumulative session average + sliding 60s window
            now = time.time()
            elapsed = now - self.session_start
            if elapsed > 0:
                self.bets_per_second = self.total_bets / elapsed
                self.bets_per_minute = self.bets_per_second * 60

            # Sliding 60s window — drop entries older than 60s and recompute
            mono = time.monotonic()
            self._recent_bet_times.append(mono)
            cutoff = mono - 60.0
            while self._recent_bet_times and self._recent_bet_times[0] < cutoff:
                self._recent_bet_times.popleft()
            self.recent_bps = len(self._recent_bet_times) / 60.0
            self.recent_bpm = self.recent_bps * 60

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

            # Per-cycle overhead = full iteration time minus the API call.
            # Excludes bet_delay sleep (we measure from start of bet, after sleep).
            # Exp-smoothed so transient spikes don't dominate the reading.
            iter_ms = (time.monotonic() - self._iter_start_mono) * 1000
            overhead = max(0.0, iter_ms - self._last_api_ms)
            self._cycle_overhead_ms = 0.9 * self._cycle_overhead_ms + 0.1 * overhead

            self._queue_bet(result, raw_profit, self.current_balance)
            await self._periodic_save()

            # profit-based base bet increment
            self._maybe_profit_increment()

            # rule-based strategy
            if self.strategy_key == "7" and self.custom_rules:
                self._apply_rules(bet_state)

            raw_next = self._compute_next_bet(bet_state)
            self.current_bet = max(self.base_bet, raw_next)
            sbl_factor = self._get_streakbet_loss_factor()
            if sbl_factor != 1.0:
                self.current_bet = self.current_bet * sbl_factor
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
        await asyncio.to_thread(self._flush_bets)
        if self.session_id:
            await asyncio.to_thread(self._db_save_session, True)
        self._close_conn()
        logger.info("User %d: Session #%d ended — %s",
                     self.user_id, self.session_id, self.stop_reason or "stopped")
        if self.on_stop:
            try:
                self.on_stop(self.stop_reason or "Stopped by user")
            except Exception:
                pass
