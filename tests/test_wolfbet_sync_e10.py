"""Run-wide profit increment (WolfBet CLI v2.37.0): shuffle CLI and TG.

Mirrors WolfBet v2.37.0 (`profit_increment_scope`, `0b7e71a`). Scope "session"
keeps today's behaviour; scope "run" measures the total since the run started,
keeps the milestone across cycles and restarts, and saves each bump in a run
file. Telegram has no lanes, so one run per user is shared by every slot.
Reference test: wolfbet/tests/test_phase10b_run_increment.py.

Run: APP_ENV=testing .venv-test/bin/python -m pytest shuffle/tests -q
"""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import shuffle as sh
from tg import engine as tg_engine, handlers as h
from core.database import init_db
from tg.database import load_user_config, save_user_config, user_db_path


def _seed(db, rows):
    """rows: [(id, profit, lane)]"""
    sh.init_db()
    conn = sqlite3.connect(db)
    for sid, profit, lane in rows:
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered, lane) "
            "VALUES (?, '2026-11-14T10:00:00', 'usdt', 'limbo', 'Flat Bet', 0.001, "
            "2.0, 4, 2, 2, ?, 0.004, ?)", (sid, profit, lane))
    conn.commit()
    conn.close()


def _cfg(scope, base=0.001, inc=0.001, thr=0.01):
    s = sh.state
    s.profit_increment_scope, s.base_bet, s.current_bet = scope, base, base
    s.profit_increment, s.profit_threshold = inc, thr
    s.next_profit_milestone = thr
    s.profit = 0.0
    s.run_first_session = s.run_since = None
    s.run_bumps = 0
    s.run_profit_done = 0.0


# ── session scope unchanged ───────────────────────────────
def test_session_scope_uses_session_profit():
    _cfg("session")
    sh.state.run_profit_done = 5.0          # ignored in session scope
    sh.state.profit = 0.005
    sh._check_profit_increment()
    assert sh.state.base_bet == pytest.approx(0.001)
    sh.state.profit = 0.01
    sh._check_profit_increment()
    assert sh.state.base_bet == pytest.approx(0.002)
    assert sh.state.next_profit_milestone == pytest.approx(0.02)


def test_init_run_noop_in_session_scope():
    _cfg("session")
    sh._init_run()
    sh._attach_run(7)
    assert sh.state.run_first_session is None
    assert not os.path.exists(sh.RUN_PATH)


# ── run scope ─────────────────────────────────────────────
def test_new_run_created_at_first_session():
    _cfg("run")
    sh._init_run()
    assert sh.state.run_first_session is None and sh.state.run_bumps == 0
    sh._attach_run(12)
    data = json.load(open(sh.RUN_PATH))
    assert data["first_session"] == 12 and data["bumps"] == 0 and data["started_at"]


def test_run_profit_sums_sessions_and_bumps_persist():
    _seed(sh.DB_PATH, [(1, 0.5, 1), (5, 0.004, 1), (6, 0.003, 1), (7, 0.9, 2)])
    json.dump({"first_session": 5, "started_at": "x", "bumps": 0}, open(sh.RUN_PATH, "w"))
    _cfg("run")
    sh._init_run()
    assert sh.state.run_profit_done == pytest.approx(0.007)   # #1 before, #7 other lane
    sh.state.profit = 0.002
    sh._check_profit_increment()
    assert sh.state.base_bet == pytest.approx(0.001)          # 0.009 < 0.01
    sh.state.profit = 0.003                                   # run total 0.010
    sh._check_profit_increment()
    assert sh.state.base_bet == pytest.approx(0.002)
    assert sh.state.next_profit_milestone == pytest.approx(0.02)
    assert json.load(open(sh.RUN_PATH))["bumps"] == 1


def test_resume_restores_bumps_on_base_bet():
    _seed(sh.DB_PATH, [(3, 0.025, 1)])
    json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, open(sh.RUN_PATH, "w"))
    _cfg("run")
    sh._init_run()
    assert sh.state.base_bet == pytest.approx(0.003)
    assert sh.state.current_bet == pytest.approx(0.003)
    assert sh.state.next_profit_milestone == pytest.approx(0.03)
    sh._attach_run(4)                        # an existing run keeps its start
    assert json.load(open(sh.RUN_PATH))["first_session"] == 3


def test_stale_run_file_after_db_reset_starts_new_run():
    _seed(sh.DB_PATH, [(1, 0.1, 1)])
    json.dump({"first_session": 5000, "started_at": "x", "bumps": 4}, open(sh.RUN_PATH, "w"))
    _cfg("run")
    sh._init_run()
    assert sh.state.run_first_session is None and sh.state.run_bumps == 0
    assert sh.state.base_bet == pytest.approx(0.001)


def test_lane_run_file_and_filter():
    _seed(sh.DB_PATH, [(2, 0.01, 1), (3, 0.02, 2)])
    sh.state.lane = 2
    json.dump({"first_session": 2, "started_at": "x", "bumps": 0},
              open(sh._run_path_for(sh.RUN_PATH, 2), "w"))
    _cfg("run")
    sh._init_run()
    assert sh.state.run_first_session == 2
    assert sh.state.run_profit_done == pytest.approx(0.02)


def test_cycle_keeps_run_total_and_milestone():
    src = open(os.path.join(os.path.dirname(sh.__file__), "shuffle.py")).read()
    body = src[src.index("def _reset_session_for_recurring"):src.index("def cmd_status")]
    assert "state.run_profit_done += state.profit" in body
    assert 'state.profit_increment_scope != "run"' in body


# ── off values ────────────────────────────────────────────
def test_zero_increment_never_bumps_or_counts():
    _cfg("run", inc=0.0)
    sh.state.run_first_session = 1
    sh.state.run_profit_done = 1.0
    sh._check_profit_increment()
    assert sh.state.run_bumps == 0 and sh.state.base_bet == pytest.approx(0.001)


@pytest.mark.parametrize("key,val", [("profit_increment", "0.0"),
                                     ("profit_increment", "none"),
                                     ("profit_threshold", "off")])
def test_mutation_zero_means_off(key, val):
    _cfg("run")
    sh._apply_mutation({key: val})
    assert getattr(sh.state, key) is None
    assert not sh._increment_on()


@pytest.mark.parametrize("inc,thr,scope", [(None, 0.01, "run"), (0.001, None, "run"),
                                           (0.001, 0.01, "session")])
def test_init_run_off_removes_run_file(inc, thr, scope):
    _seed(sh.DB_PATH, [(3, 0.05, 1)])
    json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, open(sh.RUN_PATH, "w"))
    _cfg(scope, inc=inc, thr=thr)
    sh._init_run()
    sh._attach_run(4)
    assert not os.path.exists(sh.RUN_PATH)
    assert sh.state.run_first_session is None and sh.state.run_bumps == 0
    assert sh.state.base_bet == pytest.approx(0.001)         # old bumps not re-applied


# ── config / commands ─────────────────────────────────────
def _write_cfg(**vals):
    data = {"bearer_token": "t", "currency": "usdt", "game": "limbo",
            "multiplier_target": 2.0, "base_bet": 0.001, "strategy": "Flat Bet",
            "strategy_key": "1", "purge_days": 3, "db_write_mode": "async"}
    data.update(vals)
    open(sh.CONFIG_PATH, "w").write(json.dumps(data))


def test_config_set_scope(monkeypatch):
    _write_cfg()
    monkeypatch.setattr(sh, "_apply_proxy_to_session", lambda: None)
    sh.cmd_config_set(["piscope=run"])
    assert json.load(open(sh.CONFIG_PATH))["profit_increment_scope"] == "run"
    with pytest.raises(SystemExit) as e:
        sh.cmd_config_set(["profit_increment_scope=forever"])
    assert e.value.code == 2


def test_config_show_prints_scope(capsys):
    _write_cfg(profit_increment_scope="run")
    sh.cmd_config_show()
    out = capsys.readouterr().out
    assert "profit_increment_scope" in out and "run" in out


def test_run_reset_removes_files():
    for k in (1, 3):
        open(sh._run_path_for(sh.RUN_PATH, k), "w").write("{}")
    assert sh.cmd_run_reset() == 2
    assert not os.path.exists(sh.RUN_PATH)
    open(sh._run_path_for(sh.RUN_PATH, 2), "w").write("{}")
    assert sh.cmd_run_reset(2) == 1


def test_run_reset_flag_wired():
    src = open(os.path.join(os.path.dirname(sh.__file__), "shuffle.py")).read()
    assert 'group.add_argument("--run-reset"' in src
    assert "cmd_run_reset(args.lane)" in src


def test_progress_row_uses_run_total():
    row = sh._build_config_progress_row({
        "profit_threshold": 0.01, "profit_increment": 0.001, "next_profit_milestone": 0.02,
        "profit": 0.0, "profit_increment_scope": "run", "run_profit_total": 0.015})
    assert "RunMs" in row and "50%" in row


def test_status_shows_run_profit_row(capsys, monkeypatch):
    data = {"pid": 0, "profit": 0.004, "profit_increment": 0.001,
            "profit_threshold": 0.01, "profit_increment_scope": "run",
            "run_profit_total": 0.04, "run_first_session": 5, "run_bumps": 1,
            "next_profit_milestone": 0.02, "session_id": 9}
    open(sh.STATE_PATH, "w").write(json.dumps(data))
    sh.cmd_status()
    import re
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "Run Profit" in out and "since #5" in out and "1 bump" in out


# ── live reload (CLI SIGHUP) ──────────────────────────────
def test_live_reload_increment_off_ends_run():
    _cfg("run")
    sh._attach_run(5)
    assert os.path.exists(sh.RUN_PATH)
    _write_cfg(profit_increment=None, profit_threshold=0.01,
               profit_increment_scope="run")
    sh._reload_live_config()
    assert not os.path.exists(sh.RUN_PATH) and sh.state.run_first_session is None


def test_live_reload_increment_on_starts_run():
    _cfg("run", inc=None)
    sh.state.session_id = 8
    _write_cfg(profit_increment=0.001, profit_threshold=0.01,
               profit_increment_scope="run")
    sh._reload_live_config()
    assert sh.state.run_first_session == 8
    assert json.load(open(sh.RUN_PATH))["first_session"] == 8


# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
def _tg_configured(tg_engine_factory, **cfg):
    cfg.setdefault("profit_increment", 0.001)
    cfg.setdefault("profit_threshold", 0.01)
    return tg_engine_factory(max_bets=1, **cfg)


def _tg_run_path(engine):
    return engine._run_path()


def test_tg_init_run_creates_run_at_first_session(tg_engine_factory):
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    e._init_run()
    e._attach_run(11)
    data = json.load(open(_tg_run_path(e)))
    assert data["first_session"] == 11 and data["bumps"] == 0


def test_tg_session_scope_keeps_no_run_file(tg_engine_factory):
    e = _tg_configured(tg_engine_factory)
    e._init_run()
    e._attach_run(11)
    assert e.run_first_session is None
    assert not os.path.exists(_tg_run_path(e))


def test_tg_run_scope_sums_every_slot(tg_engine_factory):
    from core.database import init_db

    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    init_db(e.db_path)
    conn = sqlite3.connect(e.db_path)
    conn.execute("INSERT INTO sessions (id, profit) VALUES (3, 0.004), (4, 0.003)")
    conn.commit()
    conn.close()
    json.dump({"first_session": 3, "started_at": "x", "bumps": 0},
              open(_tg_run_path(e), "w"))
    e._init_run()
    e.profit = 0.004
    assert e._run_others == pytest.approx(0.007)
    assert e.run_profit_total() == pytest.approx(0.011)
    e._maybe_profit_increment()
    assert e.base_bet == pytest.approx(0.002)
    assert json.load(open(_tg_run_path(e)))["bumps"] == 1


def test_tg_bump_survives_a_second_slot(tg_engine_factory):
    a = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    b = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    for e, sid in ((a, 20), (b, 20)):
        e._init_run()
        e._attach_run(sid)
    a.run_bumps = b.run_bumps = 1
    a._save_run()
    b.run_bumps = 2
    b._save_run()                        # re-reads the file: 1 then 2, never lost
    assert json.load(open(_tg_run_path(a)))["bumps"] == 2
    a._run_sync()
    assert a.run_bumps == 2              # the other slot's bump is adopted


def test_tg_resume_restores_bumps_once(tg_engine_factory):
    from core.database import init_db

    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    init_db(e.db_path)
    e._init_run()
    sid = e._db_start_session()
    e._attach_run(sid)
    e._run_base_origin = 0.001
    e.run_bumps = 2
    e._save_run()
    e.base_bet = 0.003                   # two bumps already applied
    snap = e.snapshot_state()
    assert snap["config"]["_run_base_origin"] == pytest.approx(0.001)

    restored = tg_engine.BettingEngine(1, e.db_path, snap["config"])
    restored.restore_state(snap)
    restored._init_run(resumed=True)
    assert restored.run_bumps == 2
    assert restored._run_base_origin == pytest.approx(0.001)
    assert restored.base_bet == pytest.approx(0.003)         # not 0.005
    assert restored.next_profit_milestone == pytest.approx(0.03)


@pytest.mark.parametrize("inc,thr", [(None, 0.01), (0.001, None), (0.0, 0.01)])
def test_tg_init_run_off_removes_run_file(tg_engine_factory, inc, thr):
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    e._init_run()
    e._attach_run(40)
    e.profit_increment, e.profit_threshold = inc, thr
    e._init_run()
    e._attach_run(41)
    assert not os.path.exists(_tg_run_path(e))
    assert e.run_first_session is None and e.run_bumps == 0


def test_tg_tweak_off_ends_run(tg_engine_factory):
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    e._init_run()
    e._attach_run(50)
    asyncio.run(e.mutate({"profit_increment": None}))
    assert not os.path.exists(_tg_run_path(e)) and e.run_first_session is None


def test_tg_tweak_on_starts_run(tg_engine_factory):
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run",
                       profit_increment=None)
    e._init_run()
    e.session_id = 55
    asyncio.run(e.mutate({"profit_increment": 0.001}))
    assert e.run_first_session == 55
    assert json.load(open(_tg_run_path(e)))["first_session"] == 55


def test_tg_get_status_carries_run_fields(tg_engine_factory):
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    e.run_first_session, e.run_bumps, e._run_others = 60, 1, 0.01
    s = e.get_status()
    assert s["profit_increment_scope"] == "run"
    assert s["run_first_session"] == 60 and s["run_bumps"] == 1
    assert s["run_profit_total"] == pytest.approx(0.01)


def test_tg_status_formats_run_profit(tg_engine_factory):
    from tg.formatter import format_status
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    e.run_first_session, e.run_bumps, e._run_others = 60, 1, 0.01
    text = format_status(e.get_status())
    assert "Run profit" in text and "since #60" in text


# ── TG commands / handlers ────────────────────────────────
def _update(uid, replies):
    async def reply_text(text, **k):
        replies.append(text)
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           message=SimpleNamespace(reply_text=reply_text))


def _cmd(fn, uid, *args):
    replies = []
    asyncio.run(fn(_update(uid, replies), SimpleNamespace(args=list(args))))
    return replies


def test_set_and_tweak_scope(tg_engine_factory):
    uid = 77
    save_user_config(uid, {"bearer_token": "t", "base_bet": 0.001})
    _cmd(h.cmd_set, uid, "piscope", "run")
    assert load_user_config(uid)["profit_increment_scope"] == "run"
    assert "piscope must be" in _cmd(h.cmd_set, uid, "piscope", "weekly")[0]

    e = _tg_configured(tg_engine_factory, profit_increment_scope="session")
    e.running = True
    h.active_engines[uid] = {1: e}
    try:
        _cmd(h.cmd_tweak, uid, "piscope", "run")
        assert e.profit_increment_scope == "run"
        assert "piscope must be" in _cmd(h.cmd_tweak, uid, "piscope", "weekly")[0]
    finally:
        h.active_engines.pop(uid, None)


def test_config_keys_include_scope():
    from tg.config import CONFIG_KEYS
    assert "profit_increment_scope" in CONFIG_KEYS


def test_runreset_asks_for_confirmation():
    replies = _cmd(h.cmd_runreset, 78)
    assert "Delete the run total?" in replies[0]


def test_runreset_refuses_while_running(tg_engine_factory):
    uid = 83
    e = _tg_configured(tg_engine_factory, profit_increment_scope="run")
    e.running = True
    h.active_engines[uid] = {1: e}
    try:
        replies = _cmd(h.cmd_runreset, uid)
    finally:
        h.active_engines.pop(uid, None)
    assert "Stop your sessions first" in replies[0]


def test_runreset_callback_resets_run(tg_engine_factory):
    uid = 79
    path = os.path.join(os.path.dirname(user_db_path(uid)), "run.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, f)

    replies = []

    async def answer():
        return None

    async def reply_text(text, **k):
        replies.append(text)

    update = SimpleNamespace(
        message=None,
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=uid), data="runreset_confirm",
            answer=answer, message=SimpleNamespace(reply_text=reply_text)))
    asyncio.run(h.callback_handler(update, None))

    assert "Run total reset" in replies[0]
    assert not os.path.exists(path)


def test_runreset_without_a_run_says_so():
    replies = []
    uid = 84

    async def answer():
        return None

    async def reply_text(text, **k):
        replies.append(text)

    update = SimpleNamespace(
        message=None,
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=uid), data="runreset_confirm",
            answer=answer, message=SimpleNamespace(reply_text=reply_text)))
    asyncio.run(h.callback_handler(update, None))
    assert "No run total to reset" in replies[0]


def test_runreset_callback_cancels(tg_engine_factory):
    uid = 81
    replies = []

    async def answer():
        return None

    update = SimpleNamespace(
        message=None,
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=uid), data="noop", answer=answer))
    asyncio.run(h.callback_handler(update, None))
    assert replies == []
