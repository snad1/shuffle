"""E5-S7 — parallel lanes in the shuffle CLI (WolfBet v2.35.0 / v2.36.0).

Modelled on wolfbet/tests/test_phase10_lanes.py. A lane is one process with
module-global `state`, so the "two lanes" tests run each lane with its own
BotState on one shared fake account, one after the other.
"""

import json
import re
import signal
import sqlite3

import pytest

import shuffle as sh
from shared.core.lanes import LANE_BUDGET_REASON
from shared.core.session_rules import should_restart

from .fakes import FakeCasino

BASE = {
    "bearer_token": "test-token", "currency": "usdt", "game": "limbo",
    "multiplier_target": 2.0, "dice_target": 50.5, "dice_condition": "above",
    "base_bet": 0.004, "strategy": "Martingale", "strategy_key": "2",
    "win_mult": 1.0, "loss_mult": 2.0, "bet_delay": 0, "max_wins": 10,
    "max_profit": None, "max_loss": None, "max_bets": None, "stop_on_balance": None,
    "streak_delay_loss": None, "proxy": None, "purge_days": 3, "db_write_mode": "sync",
}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _run(fn, *a, **k):
    with sh.console.capture() as cap:
        fn(*a, **k)
    return _plain(cap.get())


def _config(data=None, **extra):
    cfg = dict(BASE if data is None else data)
    cfg.update(extra)
    with open(sh.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    return cfg


def _disk():
    with open(sh.CONFIG_PATH) as f:
        return json.load(f)


def _load_lane(lane, **extra):
    _config(**extra)
    sh.state.lane = lane
    assert sh.load_config()
    return sh.state


@pytest.fixture
def no_signals(monkeypatch):
    sent = []
    monkeypatch.setattr(sh, "_signal_lane_daemons", lambda lanes=None: sent.append(lanes) or 0)
    return sent


# ── L1 identity ───────────────────────────────────────────

def test_apply_lane_switches_paths_and_log_prefix():
    base_state, base_pid = sh.STATE_PATH, sh.PID_PATH
    sh._apply_lane(4)
    assert sh.state.lane == 4
    assert ".lane4" in sh.STATE_PATH
    assert sh.STATE_PATH == sh.lane_path(base_state, 4)
    assert sh.PID_PATH == sh.lane_path(base_pid, 4)
    assert "[L4]" in sh._log_handler.formatter._fmt


def test_apply_lane_one_keeps_classic_paths():
    sh._apply_lane(None)
    assert sh.state.lane == 1
    assert sh.STATE_PATH == sh._BASE_STATE_PATH
    assert sh.PID_PATH == sh._BASE_PID_PATH
    assert "[L" not in sh._log_handler.formatter._fmt


@pytest.mark.parametrize("bad", [0, 11, -1])
def test_apply_lane_range(bad):
    with pytest.raises(ValueError):
        sh._apply_lane(bad)


def test_lane_applied_before_status(monkeypatch):
    seen = []
    monkeypatch.setattr(sh, "cmd_status", lambda: seen.append(sh.STATE_PATH))
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--status", "--lane", "2"])
    sh.main()
    assert seen == [sh.lane_path(sh._BASE_STATE_PATH, 2)]


def test_systemd_unit_per_lane():
    assert sh._systemd_unit() == "shuffle"
    sh.state.lane = 3
    assert sh._systemd_unit() == "shuffle-lane@3"


def test_each_lane_has_own_client_and_window_ids(fake_casino, monkeypatch):
    """Each lane process builds its own `_http`; windowId is fresh per Limbo bet."""
    made = []
    def fake_create():
        made.append(fake_casino.sync_session())
        return made[-1]

    monkeypatch.setattr(sh, "_create_http", fake_create)
    assert sh._create_http() is not sh._create_http()
    sh.state.current_bet, sh.state.multiplier_target, sh.state.currency = 0.001, 2.0, "usdt"
    ids = {sh._limbo_build(sh.state)[1]["data"]["windowId"] for _ in range(20)}
    assert len(ids) == 20


# ── L2 DB ─────────────────────────────────────────────────

def test_db_start_session_tags_lane():
    sh.init_db()
    sh.state.lane = 3
    sid = sh._db_start_session()
    with sqlite3.connect(sh.DB_PATH) as c:
        assert c.execute("SELECT lane FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 3


# ── L4 overrides ──────────────────────────────────────────

def test_load_config_applies_lane_overrides():
    lanes = {"count": 2, "overrides": {"2": {
        "base_bet": 0.002, "max_wins": 3, "streak_delay_loss": "3:1.5", "currency": "btc"}}}
    s = _load_lane(2, lanes=lanes)
    assert s.base_bet == s.current_bet == 0.002
    assert s.max_wins == 3
    assert s.streak_delay_loss == (3, 1.5)
    assert s.currency == "usdt"          # forbidden per lane: base wins
    sh.state = sh.BotState()
    s = _load_lane(1, lanes=lanes)
    assert s.base_bet == 0.004 and s.max_wins == 10


def test_save_config_keeps_lanes_block_and_lane_k_never_writes():
    lanes = {"count": 2, "overrides": {"2": {"max_wins": 3}}}
    _load_lane(1, lanes=lanes)
    sh.state.max_wins = 7
    sh.save_config()
    d = _disk()
    assert d["lanes"] == lanes and d["max_wins"] == 7
    sh.state = sh.BotState()
    _load_lane(2, lanes=lanes)
    sh.state.max_wins = 99
    before = open(sh.CONFIG_PATH).read()
    sh.save_config()
    assert open(sh.CONFIG_PATH).read() == before


def test_live_reload_merges_lane_overrides(caplog):
    _load_lane(2, lanes={"count": 2, "split": "bet", "overrides": {"2": {"max_wins": 3}}})
    assert sh.state.base_bet == 0.002
    cfg = _disk()
    cfg["lanes"]["overrides"]["2"]["max_wins"] = 5
    _config(cfg)
    with caplog.at_level("WARNING", logger="shuffle"):
        applied = sh._reload_live_config()
    assert applied["max_wins"] == (3, 5)
    assert sh.state.base_bet == 0.002
    assert "base_bet" not in caplog.text


# ── v2.36.0 lane split ───────────────────────────────────

def test_split_default_none_uses_full_bet():
    s = _load_lane(2, lanes={"count": 2})
    assert s.lane_split == "none" and s.base_bet == 0.004


def test_split_invalid_mode_falls_back_to_none():
    s = _load_lane(2, lanes={"count": 2, "split": "half"})
    assert s.lane_split == "none" and s.base_bet == 0.004


@pytest.mark.parametrize("mode,bet", [("budget", 0.004), ("bet", 0.002), ("both", 0.002)])
def test_split_modes_base_bet(mode, bet):
    for lane in (1, 2):
        sh.state = sh.BotState()
        s = _load_lane(lane, lanes={"count": 2, "split": mode})
        assert s.base_bet == s.current_bet == bet


def test_split_bet_ignored_with_one_lane():
    assert _load_lane(1, lanes={"count": 1, "split": "both"}).base_bet == 0.004


def test_split_bet_keeps_lane_base_bet_override():
    s = _load_lane(2, lanes={"count": 2, "split": "bet", "overrides": {"2": {"base_bet": 0.003}}})
    assert s.base_bet == 0.003


def test_split_bet_rounding_to_zero_keeps_full():
    s = _load_lane(1, base_bet=1e-8, lanes={"count": 3, "split": "bet"})
    assert s.base_bet == 1e-8


def test_split_bet_save_config_writes_full_bet():
    _load_lane(1, lanes={"count": 2, "split": "bet"})
    assert sh.state.base_bet == 0.002
    sh.save_config()
    d = _disk()
    assert d["base_bet"] == 0.004 and d["lanes"]["split"] == "bet"
    sh.state = sh.BotState()
    assert sh.load_config() and sh.state.base_bet == 0.002      # not halved twice
    sh.state.base_bet = 0.008
    sh.save_config()
    assert _disk()["base_bet"] == 0.008


def test_split_basebet_pct_divided():
    s = sh.state
    s.current_balance, s.basebet_pct, s.base_bet = 1.0, 0.01, 0.001
    s.lanes_count, s.lane_split = 2, "bet"
    assert sh._apply_basebet_pct() and s.base_bet == 0.005
    s.lane_split = "none"
    assert sh._apply_basebet_pct() and s.base_bet == 0.01


# ── config-set / config-show ──────────────────────────────

def test_config_set_lanes_and_overrides(no_signals):
    _config()
    out = _run(sh.cmd_config_set, ["lanes=2", "lane2.max_wins=3", "lane2.delay=0.5"])
    d = _disk()
    assert d["lanes"]["count"] == 2
    assert d["lanes"]["overrides"]["2"] == {"max_wins": 3, "bet_delay": 0.5}
    assert d["max_wins"] == 10 and d["bet_delay"] == 0       # base untouched
    assert no_signals == [[2]]
    assert "lane2.max_wins" in out
    out = _run(sh.cmd_config_set, ["lane2.max_wins=default"])
    assert "override removed" in out
    assert _disk()["lanes"]["overrides"]["2"] == {"bet_delay": 0.5}
    _run(sh.cmd_config_set, ["lane2.delay=inherit"])
    assert "2" not in _disk()["lanes"]["overrides"]


def test_config_set_lane_split(no_signals):
    _config()
    _run(sh.cmd_config_set, ["lanes=2", "lane_split=Both"])
    d = _disk()
    assert d["lanes"] == {"count": 2, "overrides": {}, "split": "both"}
    assert d["base_bet"] == 0.004


@pytest.mark.parametrize("pair", [
    "lane2.currency=btc", "lane2.proxy=off", "lane2.purge_days=5",
    "lane2.db_write_mode=sync", "lane2.bearer_token=x", "lane1.max_wins=3",
    "lane11.max_wins=3", "lanes=0", "lanes=x", "lane_split=half", "lane2.lane_split=bet",
    "lane2.max_wins=abc",
])
def test_config_set_bad_lane_pair_exits(pair, no_signals):
    _config()
    before = open(sh.CONFIG_PATH).read()
    with pytest.raises(SystemExit) as ex:
        _run(sh.cmd_config_set, [pair])
    assert ex.value.code == 2
    assert open(sh.CONFIG_PATH).read() == before


def test_config_set_lane_value_validated_and_derived(no_signals):
    _config(game="dice")
    _run(sh.cmd_config_set, ["lane2.multiplier=4"])
    ov = _disk()["lanes"]["overrides"]["2"]
    assert ov["multiplier_target"] == 4.0
    assert "dice_target" in ov


def test_base_config_set_signals_lane_daemons(monkeypatch):
    _config(lanes={"count": 3})
    for k, pid in ((2, 5002), (3, 5003)):
        with open(sh.lane_path(sh._BASE_PID_PATH, k), "w") as f:
            f.write(str(pid))
    kills = []
    monkeypatch.setattr(sh.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    out = _run(sh.cmd_config_set, ["maxwins=4"])
    assert _disk()["max_wins"] == 4 and _disk()["lanes"] == {"count": 3}
    assert kills == [(5002, signal.SIGHUP), (5003, signal.SIGHUP)]
    assert "Reloaded 2 running lane(s)" in out


def test_config_show_prints_lanes_split_and_overrides():
    _config(lanes={"count": 2, "split": "budget", "overrides": {"2": {"max_wins": 3}}})
    out = _run(sh.cmd_config_show)
    assert "Lanes: 2  split: budget" in out
    assert "balance ÷ lanes" in out
    assert "lane2 overrides: max_wins=3" in out


# ── L3 split budget ──────────────────────────────────────

def test_init_lane_budget_per_mode():
    s = sh.state
    s.current_balance, s.lanes_count = 1.0, 4
    for mode, share in (("none", None), ("bet", None), ("budget", 0.25), ("both", 0.25)):
        s.lane_split = mode
        sh._init_lane_budget()
        assert s.lane_share == share
    s.lanes_count, s.lane_split = 1, "budget"
    sh._init_lane_budget()
    assert s.lane_share is None


def _budget_state(share, run_profit, profit, bet):
    s = sh.state
    s.max_profit = s.max_loss = s.max_bets = s.max_wins = s.stop_on_balance = None
    s.lane_share, s.lane_run_profit, s.profit, s.current_bet = share, run_profit, profit, bet
    s.current_streak = -3
    return s


def test_budget_stop_when_next_bet_exceeds_equity():
    _budget_state(0.01, -0.001, -0.002, 0.008)
    stop, reason = sh.should_stop()
    assert stop and reason.startswith(LANE_BUDGET_REASON)
    assert not should_restart(reason, recurring=True, streak_reset_losses=3, profit=1.0)


def test_budget_ok_within_equity():
    _budget_state(0.01, -0.003, -0.002, 0.004)
    assert sh.should_stop() == (False, "")


def test_no_budget_without_lanes():
    _budget_state(None, 0.0, -5.0, 99.0)
    assert sh.should_stop() == (False, "")


def test_recurring_cycle_carries_lane_profit():
    s = sh.state
    sh.init_db()
    s.running, s.profit, s.lane_run_profit = True, 0.003, 0.001
    sh._reset_session_for_recurring(0)
    assert s.profit == 0.0 and s.lane_run_profit == pytest.approx(0.004)


def test_risky_config_uses_lane_share():
    s = sh.state
    s.current_balance, s.base_bet, s.loss_mult = 1.0, 0.001, 2.0
    s.strategy_key, s.streak_reset_losses, s.max_loss = "2", 3, None
    s.lanes_count, s.lane_split = 4, "budget"
    assert any("lane share covers a" in w for w in sh._check_risky_config())
    s.lane_split = "none"
    assert any("balance covers a" in w for w in sh._check_risky_config())


def test_state_file_writes_lane_fields():
    s = sh.state
    s.lane, s.lanes_count, s.lane_split, s.lane_share = 2, 2, "both", 0.5
    s.lane_run_profit, s.profit = 0.1, -0.05
    sh._save_state_file()
    with open(sh.STATE_PATH) as f:
        d = json.load(f)
    assert (d["lane"], d["lanes_count"], d["lane_split"], d["lane_share"]) == (2, 2, "both", 0.5)
    assert d["lane_equity"] == pytest.approx(0.55)


# ── L5 start stagger ─────────────────────────────────────

class _Started(Exception):
    pass


def test_start_stagger_and_share(monkeypatch, sleeps):
    _config(lanes={"count": 3, "split": "budget"})
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--resume", "--lane", "3"])

    def connect():
        assert sh.load_config()
        sh.state.current_balance = 9.0

    def started():
        raise _Started

    monkeypatch.setattr(sh, "_load_and_connect", connect)
    monkeypatch.setattr(sh, "_db_start_session", started)
    with pytest.raises(_Started):
        sh.main()
    assert sleeps == [4.0]
    assert sh.state.lane_share == 3.0


def test_lane_one_has_no_stagger(monkeypatch, sleeps):
    _config()
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--resume"])
    monkeypatch.setattr(sh, "_load_and_connect", lambda: sh.load_config())
    monkeypatch.setattr(sh, "_db_start_session", lambda: (_ for _ in ()).throw(_Started()))
    with pytest.raises(_Started):
        sh.main()
    assert sleeps == [] and sh.state.lane_share is None


# ── two lanes on one account ─────────────────────────────

def test_two_lanes_independent_chains(monkeypatch):
    """Lane 1 loses then wins, lane 2 only wins: each Martingale ladder and
    session row follows its own results, on one shared account."""
    casino = FakeCasino(balance=10.0)
    monkeypatch.setattr(sh, "_create_http", casino.sync_session)
    _config(lanes={"count": 2, "split": "both"}, max_wins=1, base_bet=0.004)
    sh.init_db()
    results = {}
    for lane, outcomes in ((1, ("loss", "loss", "win")), (2, ("win",))):
        sh.state = sh.BotState()
        sh.state.lane = lane
        monkeypatch.setattr(sh, "_http", casino.sync_session())
        assert sh.load_config()
        sh.state.start_balance = sh.state.current_balance = casino.balance
        sh._init_lane_budget()
        sh.state.session_id = sh._db_start_session()
        sh.state.running = True
        casino.queue(*outcomes)
        sh.betting_loop()
        results[lane] = sh.state
    l1, l2 = results[1], results[2]
    bets = [b["amount"] for b in casino.bets]
    assert bets == [0.002, 0.004, 0.008, 0.002]     # split bet, lane 1 ladder, lane 2 fresh
    assert (l1.wins, l1.losses, l2.wins, l2.losses) == (1, 2, 1, 0)
    assert l1.lane_share == 5.0 and l2.lane_share == pytest.approx(10.002 / 2)   # after lane 1 netted +0.002
    with sqlite3.connect(sh.DB_PATH) as c:
        rows = c.execute("SELECT lane, total_bets FROM sessions ORDER BY id").fetchall()
    assert rows == [(1, 3), (2, 1)]


def test_lane_budget_stops_lane_on_fake(monkeypatch):
    casino = FakeCasino(balance=0.02)
    monkeypatch.setattr(sh, "_http", casino.sync_session())
    _config(lanes={"count": 2, "split": "budget"}, max_wins=None, base_bet=0.004)
    sh.init_db()
    sh.state.lane = 2
    assert sh.load_config()
    sh.state.start_balance = sh.state.current_balance = casino.balance
    sh._init_lane_budget()                             # share 0.01
    sh.state.session_id = sh._db_start_session()
    sh.state.running = True
    sh.state.recurring = True
    casino.queue("loss", "loss", "win")
    sh.betting_loop()
    assert casino.bet_posts == 1                       # 0.004 lost, next 0.008 > 0.006
    assert not sh.state.running
    assert LANE_BUDGET_REASON in sh.state.status


# ── L6 benchmark ─────────────────────────────────────────

def test_benchmark_lanes_table_and_recommendation(fake_casino, monkeypatch):
    _config()
    monkeypatch.setattr(sh, "_create_http", fake_casino.sync_session)
    out = _run(sh.cmd_benchmark, 3, lanes=3)
    assert "Concurrent clients" in out
    assert "Recommended lanes:" in out
    ops = {b.get("operationName") for _, _, b in fake_casino.calls}
    assert ops == {"GetMyProfile"}
    assert len(fake_casino.calls) == 3 * (1 + 2 + 3)


def test_bench_lanes_counts_429(monkeypatch):
    class R:
        status_code = 429

    class S:
        def post(self, *a, **k):
            return R()

        def close(self):
            pass

    monkeypatch.setattr(sh, "_create_http", S)
    r = sh._bench_lanes(4, 2)
    assert r["s429"] == 8 and r["ok"] == 0 and r["total"] == 8


def test_benchmark_lanes_arg_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(sh, "cmd_benchmark", lambda n, lanes=None: calls.append((n, lanes)))
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--benchmark", "10", "--lanes", "4"])
    sh.main()
    assert calls == [(10, 4)]
