"""Test sandbox + fake casino fixtures.

Module top runs before any test imports `shuffle` or `tg`: path constants are
bound at import from HOME / SHUFFLE_TG_DATA, so they must point at a temp dir
first. Each test then gets its own tmp paths, a fresh CLI `state`, and
non-waiting sleeps.
"""

import asyncio
import os
import sys
import tempfile
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CASINO = os.path.dirname(_ROOT)
for _p in (_ROOT, _CASINO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SANDBOX_HOME = tempfile.mkdtemp(prefix="shuffle_home_")
os.environ["HOME"] = _SANDBOX_HOME
os.environ["APP_ENV"] = "testing"
os.environ.setdefault("SHUFFLE_WEB_SECRET_KEY", "test-secret-key-for-the-suite-12345678")
os.environ.setdefault("SHUFFLE_WEB_ADMIN_PASS", "test-admin-pass-1234")
os.environ["SHUFFLE_TG_DATA"] = os.path.join(_SANDBOX_HOME, "tg")

import shuffle as sh  # noqa: E402
from tg import engine as tg_engine, database as tg_database  # noqa: E402

from .fakes import FakeCasino  # noqa: E402

_real_async_sleep = asyncio.sleep


@pytest.fixture
def sleeps():
    """Durations the code asked to sleep (CLI time.sleep + TG asyncio.sleep)."""
    return []


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch, sleeps):
    for name in ("DB_PATH", "CONFIG_PATH", "STATE_PATH", "PID_PATH", "PRESET_PATH",
                 "RUN_PATH"):
        monkeypatch.setattr(sh, name, str(tmp_path / f"cli_{name.lower()}"))
    # v1.10.0 lanes: lane paths derive from the lane-1 paths; `_apply_lane`
    # swaps the log formatter, restored after each test.
    monkeypatch.setattr(sh, "_BASE_STATE_PATH", sh.STATE_PATH)
    monkeypatch.setattr(sh, "_BASE_PID_PATH", sh.PID_PATH)
    _fmt = sh._log_handler.formatter
    monkeypatch.setattr(tg_database, "DATA_DIR", str(tmp_path / "tg"))
    monkeypatch.setattr(sh, "state", sh.BotState())

    async def fast_sleep(delay, *a, **k):
        sleeps.append(delay)
        await _real_async_sleep(0)

    monkeypatch.setattr(sh.time, "sleep", sleeps.append)
    monkeypatch.setattr(tg_engine.asyncio, "sleep", fast_sleep)
    yield
    sh._log_handler.setFormatter(_fmt)


@pytest.fixture
def fake_casino():
    return FakeCasino(balance=10.0)


@pytest.fixture
def cli_state(fake_casino, monkeypatch):
    """Factory: configured CLI BotState with a started session on the fake."""
    def make(game="limbo", max_bets=10, **overrides):
        session = fake_casino.sync_session()
        monkeypatch.setattr(sh, "_http", session)
        monkeypatch.setattr(sh, "_create_http", lambda: session)
        sh.init_db()
        s = sh.state
        s.bearer_token = "test-token"
        s.currency = "usdt"
        s.game = game
        s.multiplier_target = 2.0
        s.dice_target = 50.5
        s.dice_condition = "above"
        s.strategy = "Flat Bet"
        s.strategy_key = "1"
        s.base_bet = s.current_bet = sh.MIN_BET
        s.max_bets = max_bets
        bal = fake_casino.balance
        s.start_balance = s.current_balance = s.highest_balance = s.lowest_balance = bal
        for k, v in overrides.items():
            setattr(s, k, v)
        s.session_id = sh._db_start_session()
        s.session_start = time.time()
        s.running = True
        return s
    return make


@pytest.fixture
def run_cli():
    """Run the real CLI betting loop on the main thread until it stops."""
    def run():
        sh.betting_loop()
        return sh.state
    return run


@pytest.fixture
def tg_engine_factory(fake_casino, tmp_path, monkeypatch):
    """Factory: real TG BettingEngine whose HTTP client is the fake casino."""
    async def fake_recreate(self):
        if self._http:
            await self._http.aclose()
        self._http = fake_casino.async_client()

    monkeypatch.setattr(tg_engine.BettingEngine, "_recreate_http", fake_recreate)

    def make(game="limbo", max_bets=10, **cfg):
        config = {
            "bearer_token": "test-token",
            "game": game, "currency": "usdt", "multiplier_target": 2.0,
            "dice_target": 50.5, "dice_condition": "above",
            "base_bet": tg_engine.MIN_BET, "strategy": "Flat Bet", "strategy_key": "1",
            "max_bets": max_bets,
        }
        config.update(cfg)
        e = tg_engine.BettingEngine(user_id=1, db_path=str(tmp_path / "tg.db"), config=config)
        e.stops, e.errors = [], []
        e.on_stop = e.stops.append
        e.on_error = e.errors.append
        return e
    return make


@pytest.fixture
def run_tg():
    """Start the engine and wait for its betting task to finish."""
    def run(engine):
        async def _run():
            assert await engine.start(), engine.last_error
            await engine._task
        asyncio.run(_run())
        return engine
    return run
