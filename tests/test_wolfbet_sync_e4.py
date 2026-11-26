"""Shuffle CLI stats and analytics (WolfBet CLI v2.23.0 to v2.35.0).

Mirrors wolfbet tests test_phase8_analytics, test_phase6d_reset_and_datestats (stats part),
test_phase6e_analytics_dates, test_phase9c_stats_window_avgs, test_phase9d_durations and
test_phase10_lanes (stats / analytics lane filter), on a seeded temp DB.

Run: APP_ENV=testing .venv-test/bin/python -m pytest shuffle/tests -q
"""

import json
import re
import sqlite3
import time

import pytest

import shuffle as sh

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text):
    return _ANSI.sub("", text)


def _seed(rows):
    """rows: (id, started, ended, game, strategy, bets, wins, losses, profit, lane)."""
    sh.init_db()
    conn = sqlite3.connect(sh.DB_PATH)
    for sid, started, ended, game, strat, bets, wins, losses, profit, lane in rows:
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, "
            "base_bet, multiplier, start_balance, end_balance, total_bets, wins, losses, "
            "profit, wagered, lane) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, started, ended, "usdt", game, strat, 0.0001, 2.0, 1.0, 1.0 + profit,
             bets, wins, losses, profit, bets * 0.0001, lane))
    conn.commit()
    conn.close()


def _run(fn, *a, **k):
    with sh.console.capture() as cap:
        fn(*a, **k)
    return _plain(cap.get())


DAY = [
    (1, "2026-11-13T13:00:00", "2026-11-13T13:10:00", "limbo", "Flat",       10, 6, 4,  0.0002, 1),
    (2, "2026-11-13T14:00:00", "2026-11-13T14:20:00", "dice",  "Martingale", 30, 10, 20, -0.0010, 1),
    (3, "2026-11-13T15:00:00", "2026-11-13T15:30:00", "limbo", "Flat",       20, 14, 6,  0.0008, 2),
]


# ── stats window ──────────────────────────────────────────
def test_stats_time_of_day_window_excludes_earlier_rows():
    _seed(DAY)
    out = _run(sh.cmd_stats, since="2026-11-13 14:00")
    assert "Session #2" in out and "Session #3" in out
    assert "Session #1" not in out
    assert "Range Totals" in out


def test_stats_invalid_bound_prints_error_and_no_rows():
    _seed(DAY)
    out = _run(sh.cmd_stats, since="13/11/2026")
    assert 'Invalid --since/--until "13/11/2026"' in out
    assert "Session #" not in out


def test_stats_wins_losses_per_session():
    _seed(DAY)
    out = _run(sh.cmd_stats)
    assert "All-Time Totals" in out
    assert "Wins/Session:  Max 14  Avg 10.0" in out
    assert "Losses/Session: Max 20  Avg 10.0" in out


def test_stats_time_span_vs_bet_time_with_gap():
    _seed(DAY)
    out = _run(sh.cmd_stats)
    # 13:00 → 15:30 span; bet time = 10m + 20m + 30m (gaps excluded)
    assert "Time Span:     2h 30m 0s" in out
    assert "Bet Time: 1h 0m 0s" in out
    assert "(2026-11-13 13:00:00 → 2026-11-13 15:30:00)" in out


def test_stats_no_db_file():
    out = _run(sh.cmd_stats)
    assert "No database found" in out


# ── lanes ─────────────────────────────────────────────────
def test_stats_lane_filter_and_tag():
    _seed(DAY)
    out = _run(sh.cmd_stats, lane=2)
    assert "Session #3 [L2]" in out
    assert "Session #1" not in out and "Session #2" not in out
    assert "[lane 2]" in out
    assert "Range Totals" in out
    lane1 = _run(sh.cmd_stats, lane=1)
    assert "Session #1" in lane1 and "Session #2" in lane1 and "Session #3" not in lane1
    assert "[L1]" not in lane1


def test_init_db_adds_lane_to_old_db_idempotent():
    conn = sqlite3.connect(sh.DB_PATH)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT, "
                 "currency TEXT, game TEXT, strategy TEXT, total_bets INTEGER, wins INTEGER, "
                 "losses INTEGER, profit REAL, wagered REAL)")
    conn.execute("INSERT INTO sessions VALUES (1,'2026-11-13T10:00:00','2026-11-13T10:05:00',"
                 "'usdt','limbo','Flat',5,3,2,0.0001,0.0005)")
    conn.commit()
    conn.close()
    sh.init_db()
    sh.init_db()
    conn = sqlite3.connect(sh.DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    lane = conn.execute("SELECT lane FROM sessions WHERE id=1").fetchone()[0]
    conn.close()
    assert cols.count("lane") == 1
    assert lane == 1


# ── analytics ─────────────────────────────────────────────
def test_analytics_blocks():
    _seed(DAY)
    out = _run(sh.cmd_analytics)
    assert "Lifetime analytics" in out and "3 sessions, 60 bets" in out
    for block in ("Overall", "By game", "By strategy", "Top 5 best sessions", "Top 5 worst sessions"):
        assert block in out
    assert "Time range:" in out and "2026-11-13 13:00:00 → 2026-11-13 15:30:00" in out
    assert "Time span:" in out and "2h 30m 0s" in out
    assert "Bet time:" in out and "1h 0m 0s" in out
    assert "Sessions in profit:  2" in out and "Sessions in loss:    1" in out
    assert "Limbo" in out and "Dice" in out           # GAMES labels, not raw keys
    assert "Martingale" in out


def test_analytics_by_lane_shown_only_with_two_lanes():
    _seed(DAY)
    out = _run(sh.cmd_analytics)
    assert "By lane" in out and "Lane 1" in out and "Lane 2" in out
    rows = {r["lane"]: r for r in sh._analytics_by_lane()}
    assert rows["Lane 1"]["sessions"] == 2 and rows["Lane 1"]["bets"] == 40
    assert rows["Lane 2"]["sessions"] == 1 and rows["Lane 2"]["bets"] == 20
    one = _run(sh.cmd_analytics, lane=2)
    assert "By lane" not in one
    assert "1 sessions, 20 bets" in one and "[lane 2]" in one


def test_analytics_window_and_bad_bound():
    _seed(DAY)
    out = _run(sh.cmd_analytics, since="2026-11-13 14:00", until="2026-11-13 14:30")
    assert "Range analytics" in out and "1 sessions, 30 bets" in out
    bad = _run(sh.cmd_analytics, until="tomorrow")
    assert 'Invalid --since/--until "tomorrow"' in bad
    assert "Overall" not in bad


def test_analytics_empty_db():
    sh.init_db()
    out = _run(sh.cmd_analytics)
    assert "No sessions in the DB yet" in out


def test_analytics_top_sessions_order():
    _seed(DAY)
    best = sh._analytics_top_sessions("profit_desc", 5)
    worst = sh._analytics_top_sessions("profit_asc", 5)
    assert [r["id"] for r in best] == [3, 1, 2]
    assert [r["id"] for r in worst] == [2, 1, 3]
    assert sh._analytics_top_sessions("drop table", 1)[0]["id"] == 3   # whitelisted order


# ── run time headers ──────────────────────────────────────
def test_tui_header_shows_run_time():
    sh.state.session_start = time.time() - 30
    sh.state.run_started_at = time.time() - 3725
    head = _plain(sh.build_dashboard_screen()).splitlines()[0]
    assert "Run 1h 2m 5s" in head


def _state_doc(**extra):
    d = {"session_id": 4, "uptime_sec": 60, "currency": "usdt", "strategy": "Flat",
         "multiplier_target": 2.0, "game": "limbo"}
    d.update(extra)
    return d


def test_monitor_header_run_time_only_when_present():
    with_run = _plain(sh._build_monitor_screen(_state_doc(run_uptime_sec=90061), [], True))
    assert "Run 1d 1h 1m 1s" in with_run.splitlines()[0]
    old = _plain(sh._build_monitor_screen(_state_doc(), [], True))
    assert "Run " not in old.splitlines()[0]


def test_status_running_for(tmp_path):
    with open(sh.STATE_PATH, "w") as f:
        json.dump(_state_doc(run_uptime_sec=125), f)
    out = _run(sh.cmd_status)
    assert "Running for" in out and "2m 5s" in out
    with open(sh.STATE_PATH, "w") as f:
        json.dump(_state_doc(), f)
    assert "Running for" not in _run(sh.cmd_status)


# ── argparse ──────────────────────────────────────────────
def test_cli_analytics_and_lane_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(sh, "cmd_analytics", lambda **k: calls.append(("a", k)))
    monkeypatch.setattr(sh, "cmd_stats", lambda **k: calls.append(("s", k)))
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--analytics", "--since", "2026-11-13 14:00",
                                         "--lane", "2"])
    sh.main()
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--stats", "--lane", "3"])
    sh.main()
    assert calls == [("a", {"since": "2026-11-13 14:00", "until": None, "lane": 2}),
                     ("s", {"since": None, "until": None, "lane": 3, "as_json": False, "before": None})]


def test_cli_lane_out_of_range_exits(monkeypatch):
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--stats", "--lane", "100"])
    with sh.console.capture() as cap, pytest.raises(SystemExit) as ex:
        sh.main()
    assert ex.value.code == 1
    assert "--lane must be 1-99, got 100" in _plain(cap.get())


def test_cli_stats_analytics_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--stats", "--analytics"])
    with pytest.raises(SystemExit) as ex:
        sh.main()
    assert ex.value.code == 2
