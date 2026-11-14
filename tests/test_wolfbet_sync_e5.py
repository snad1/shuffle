"""E5-S2 — shuffle CLI observability and tooling (WolfBet CLI v2.17.0 … v2.30.5).

Mirrors wolfbet tests test_phase3_metrics, test_phase5i_diagnostics, test_phase5g_proxy,
test_phase5h_purge_days, test_phase5j_async_writer, test_phase4a_presets,
test_phase4b_mutation, test_phase5d_config_cli, test_phase7_polish (+ the chain-depth
case of test_phase9b), test_phase5w/5x/5z and test_phase5m_monitor_ux, on the E0 fake
casino. Lanes, pipeline, rate-limit headers and auto-bet UUIDs are N/A here.

Run: APP_ENV=testing .venv-test/bin/python -m pytest shuffle/tests -q
"""

import inspect
import json
import queue as _queue
import re
import signal
import sqlite3
from datetime import datetime, timedelta

import pytest
import requests

import shuffle as sh

B = sh.MIN_BET


def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _run(fn, *a, **k):
    with sh.console.capture() as cap:
        fn(*a, **k)
    return _plain(cap.get())


def _save_min_config(**extra):
    s = sh.state
    s.bearer_token = "tok-abcdefghijklmnopqrstuvwxyz"
    for k, v in extra.items():
        setattr(s, k, v)
    sh.save_config()


@pytest.fixture
def writer_reset():
    """Leave no writer thread, queued rows or stats behind."""
    yield
    sh._stop_bet_writer(timeout=2)
    while not sh._bet_write_queue.empty():
        sh._bet_write_queue.get_nowait()
    for k in sh._bet_writer_stats:
        sh._bet_writer_stats[k] = 0


# ── 1. metrics ────────────────────────────────────────────
def test_metric_helpers_empty_are_zero():
    assert sh._compute_api_avg() == 0
    assert sh._compute_api_p50() == 0
    assert sh._compute_api_p99() == 0
    assert sh._compute_on_target_pct() is None
    assert sh._compute_cycle_p50() == 0
    assert sh._compute_recent_bps() == 0
    assert sh._compute_theoretical_max_bps() == 0
    assert sh._compute_efficiency_pct() == 0


def test_record_api_ms_counters_and_on_target():
    sh.state.bet_delay = 0.1
    for ms in (50, 80, 150):
        sh._record_api_ms(ms)
    s = sh.state
    assert s._last_api_ms == 150 and s.peak_api_ms == 150 and s._api_ms_count == 3
    assert sh._compute_api_avg() == pytest.approx(280 / 3)
    assert sh._compute_api_p50() == 80
    assert sh._compute_on_target_pct() == pytest.approx(200 / 3)


def test_api_window_saturates_at_500():
    for i in range(600):
        sh._record_api_ms(1000 if i < 100 else 10)
    assert len(sh.state._api_samples) == 500
    assert sh._compute_api_rolling_avg() == 10      # cold-start spikes rolled out
    assert sh._compute_api_avg() > 10               # all-time mean keeps them


def test_cycle_p50_stable_on_outlier_and_overhead_floor():
    sh._record_api_ms(40)
    for ms in (50, 50, 50, 50, 400):
        sh._record_cycle_ms(ms)
    assert sh._compute_cycle_p50() == 50
    assert sh._compute_cycle_p99() == 400
    assert sh.state._cycle_ms == 50
    assert sh.state._cycle_overhead_ms == pytest.approx(10)
    sh.state._cycle_samples.clear()
    sh._record_cycle_ms(5)                          # cycle < api -> overhead floors at 0
    assert sh.state._cycle_overhead_ms == 0


def test_recent_bps_uses_mean_and_efficiency_caps():
    for ms in (100, 100, 100, 500):
        sh._record_cycle_ms(ms)
    assert sh._compute_recent_bps() == pytest.approx(1000 / 200)
    sh._record_api_ms(100)
    assert sh._compute_theoretical_max_bps() == pytest.approx(10)
    assert sh._compute_efficiency_pct() == pytest.approx(50)
    sh.state._cycle_samples.clear()
    sh._record_cycle_ms(50)                         # faster than the ceiling
    assert sh._compute_efficiency_pct() == 100


def test_theoretical_max_bounded_by_delay():
    sh.state.bet_delay = 0.5
    sh._record_api_ms(100)
    assert sh._compute_theoretical_max_bps() == pytest.approx(2.0)


def test_bet_loop_fills_metrics_and_state_file(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "loss", "win")
    cli_state(max_bets=3)
    run_cli()
    s = sh.state
    assert s._api_ms_count == 3 and len(s._cycle_samples) == 3
    sh._save_state_file()
    with open(sh.STATE_PATH) as f:
        d = json.load(f)
    for k in ("api_ms", "api_avg_ms", "api_peak_ms", "api_p50_ms", "api_p99_ms", "on_target_pct",
              "cycle_ms", "overhead_ms", "cycle_p50_ms", "cycle_p99_ms", "cycle_mean_ms",
              "cycle_sample_count", "bets_per_second_recent", "efficiency_pct",
              "theoretical_max_bps", "db_write_mode", "db_writer_stats", "db_writer_queue",
              "purge_days", "proxy"):
        assert k in d, k
    assert d["cycle_sample_count"] == 3
    assert d["proxy"] == "direct"


def test_dashboard_and_status_show_metrics(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "win")
    cli_state(max_bets=2)
    run_cli()
    screen = _plain(sh.build_dashboard_screen())
    assert "API" in screen and "p50" in screen and "Efficiency" in screen and "Cycle" in screen
    assert "[T]weak" in screen
    sh._record_api_ms(30)          # the fake answers in ~0 ms; give the rows a value
    sh._record_cycle_ms(40)
    sh._save_state_file()
    out = _run(sh.cmd_status)
    assert "API" in out and "Cycle" in out


# ── 2. benchmark / diagnose ───────────────────────────────
def test_benchmark_probes_only_get_my_profile(fake_casino, cli_state):
    cli_state()
    _save_min_config()
    out = _run(sh.cmd_benchmark, None)
    ops = [b.get("operationName") for _, _, b in fake_casino.calls]
    assert ops == ["GetMyProfile"] * 20
    assert fake_casino.bet_posts == 0
    assert "API latency" in out and "20/20" in out


@pytest.mark.parametrize("n,expected", [(1, 3), (500, 200), (7, 7)])
def test_benchmark_clamps(fake_casino, cli_state, n, expected):
    cli_state()
    _save_min_config()
    _run(sh.cmd_benchmark, n)
    assert len(fake_casino.calls) == expected


def test_benchmark_429_is_rate_limited(cli_state, monkeypatch):
    cli_state()
    _save_min_config()

    class R:
        status_code = 429

    class S:
        proxies = {}

        def post(self, *a, **k):
            return R()

    monkeypatch.setattr(sh, "_http", S())
    out = _run(sh.cmd_benchmark, 3)
    assert "rate" in out.lower() and "Shuffle" in out


def test_benchmark_no_config_exits():
    with pytest.raises(SystemExit) as ex:
        _run(sh.cmd_benchmark, 5)
    assert ex.value.code == 1


def test_diagnose_without_state_file():
    assert "No running daemon session found" in _run(sh.cmd_diagnose)


def _write_state(**kw):
    d = {"game": "limbo", "strategy": "Flat Bet", "bets_per_second": 5.0,
         "api_p50_ms": 20, "overhead_ms": 5, "cycle_ms": 25, "bet_delay": 0,
         "theoretical_max_bps": 50, "db_write_mode": "async",
         "db_writer_stats": {"fallback_sync": 0, "queue_high_watermark": 1}}
    d.update(kw)
    with open(sh.STATE_PATH, "w") as f:
        json.dump(d, f)


def test_diagnose_network_blocker_advises_benchmark():
    _write_state(api_p50_ms=300)
    out = _run(sh.cmd_diagnose)
    assert "Top blocker: Network / API latency" in out
    assert "--benchmark" in out


def test_diagnose_delay_blocker_and_overflow_and_over_100():
    _write_state(bet_delay=1.0, bets_per_second_recent=80,
                 db_writer_stats={"fallback_sync": 4, "queue_high_watermark": 1000})
    out = _run(sh.cmd_diagnose)
    assert "Top blocker: User-configured delay" in out
    assert "delay=0" in out and "overflowed 4 times" in out and ">100%" in out
    assert "pipeline" not in out.lower() and "auto/start" not in out


def test_diagnose_sync_overhead_advises_async():
    _write_state(overhead_ms=200, db_write_mode="sync")
    assert "dbmode=async" in _run(sh.cmd_diagnose)


# ── 3. retention ──────────────────────────────────────────
def test_purge_days_mutation_clamps_and_off():
    assert sh.state.purge_days == 3
    msgs = sh._apply_mutation({"purge_days": "0"})
    assert sh.state.purge_days == 1 and any(m.startswith("⚠ purge_days clamped") for m in msgs)
    sh._apply_mutation({"purge_days": "99"})
    assert sh.state.purge_days == 30
    sh._apply_mutation({"purge_days": "off"})
    assert sh.state.purge_days == 3
    with pytest.raises(ValueError):
        sh._apply_mutation({"purge_days": "abc"})


def _seed_old_session(db, days_ago, bets=3):
    conn = sqlite3.connect(db)
    ended = (datetime.now() - timedelta(days=days_ago)).isoformat()
    cur = conn.execute("INSERT INTO sessions (started_at, ended_at) VALUES (?, ?)", (ended, ended))
    sid = cur.lastrowid
    for _ in range(bets):
        conn.execute("INSERT INTO bets (session_id, timestamp, game, amount, state) "
                     "VALUES (?, ?, 'limbo', 1, 'win')", (sid, ended))
    conn.commit()
    conn.close()
    return sid


def _bet_count(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    finally:
        conn.close()


def test_cleanup_purges_old_session_bets_keeps_sessions():
    sh.init_db()
    _seed_old_session(sh.DB_PATH, 10)
    _seed_old_session(sh.DB_PATH, 1)
    out = _run(sh.cmd_cleanup, 5)
    assert "Purged 3 bet rows" in out
    assert _bet_count(sh.DB_PATH) == 3
    conn = sqlite3.connect(sh.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    conn.close()
    assert "Nothing to purge" in _run(sh.cmd_cleanup, 5)


def test_cleanup_uses_purge_days_and_clamps(monkeypatch):
    seen = []
    monkeypatch.setattr(sh, "cleanup_old_bets",
                        lambda db, bet_age_days: seen.append(bet_age_days) or 0)
    _save_min_config(purge_days=7)
    _run(sh.cmd_cleanup, None)
    _run(sh.cmd_cleanup, 0)
    _run(sh.cmd_cleanup, 9999)
    assert seen == [7, 1, 30]


def test_retention_worker_exits_when_stopped():
    sh.state.running = sh.state.paused = False
    sh._retention_worker()   # returns at once


# ── 4. proxy ──────────────────────────────────────────────
def test_proxy_validate_and_mask():
    assert sh.validate_proxy("off") is None
    assert sh.validate_proxy("socks5://u:p@h:1080") == "socks5://u:p@h:1080"
    with pytest.raises(ValueError):
        sh.validate_proxy("ftp://h")
    assert sh.mask_proxy("http://user:pass@host:8080") == "http://host:8080"
    assert sh.mask_proxy(None) == "direct"


@pytest.mark.parametrize("has_cffi", [True, False])
def test_create_http_applies_proxy_both_clients(monkeypatch, has_cffi):
    if has_cffi and not sh._HAS_CFFI:
        pytest.skip("curl_cffi not installed")
    monkeypatch.setattr(sh, "_HAS_CFFI", has_cffi)
    sh.state.proxy = "socks5://u:p@h:1080"
    sess = sh._create_http()
    assert sess.proxies == {"http": "socks5://u:p@h:1080", "https": "socks5://u:p@h:1080"}
    sh.state.proxy = None
    assert sh._create_http().proxies == {}


def test_proxy_kept_after_connection_reset_recreate(fake_casino, monkeypatch):
    """The `_gql_post` recreate on a connection reset keeps the proxy."""
    made = []

    class Sess:
        def __init__(self, *a, **k):
            self.proxies = {}
            made.append(self)

        def post(self, url, headers=None, json=None, timeout=None, **_):
            return fake_casino.sync_session().post(url, headers=headers, json=json)

        def close(self):
            pass

    class Broken(Sess):
        def post(self, *a, **k):
            raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(sh, "_HAS_CFFI", False)
    monkeypatch.setattr(sh.requests, "Session", Sess)
    sh.state.proxy = "https://h:3128"
    monkeypatch.setattr(sh, "_http", Broken())
    data = sh._gql_post(sh._PROFILE_QUERY, {}, "GetMyProfile")
    assert data["me"]["account"]["balances"]
    assert sh._http is made[-1] and not isinstance(sh._http, Broken)
    assert sh._http.proxies == {"http": "https://h:3128", "https": "https://h:3128"}


def test_proxy_mutation_applies_and_masks(monkeypatch):
    class S:
        proxies = {}
    monkeypatch.setattr(sh, "_http", S())
    msgs = sh._apply_mutation({"proxy": "http://user:secret@h:8080"})
    assert sh._http.proxies["https"] == "http://user:secret@h:8080"
    assert msgs == ["Proxy: http://h:8080"]
    sh._apply_mutation({"proxy": "off"})
    assert sh.state.proxy is None and sh._http.proxies == {}
    with pytest.raises(ValueError, match="proxy"):
        sh._apply_mutation({"proxy": "ftp://h"})


def test_saved_proxy_applied_on_load_and_bad_one_dropped(monkeypatch):
    class S:
        proxies = {}
    monkeypatch.setattr(sh, "_http", S())
    _save_min_config(proxy="socks5://h:1080")
    sh.state = sh.BotState()
    assert sh.load_config()
    assert sh._http.proxies["http"] == "socks5://h:1080"
    with open(sh.CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["proxy"] = "ftp://bad"
    with open(sh.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    sh.state = sh.BotState()
    with sh.console.capture():
        assert sh.load_config()
    assert sh.state.proxy is None and sh._http.proxies == {}


def test_no_bare_requests_calls():
    src = inspect.getsource(sh)
    assert not re.search(r"\brequests\.(get|post|put|delete)\(", src)


def test_risky_warns_on_cleartext_proxy_only():
    sh.state.proxy = "http://h:1"
    assert any("http://" in w for w in sh._check_risky_config())
    sh.state.proxy = "socks5://h:1"
    assert not any("proxy" in w for w in sh._check_risky_config())


# ── 5. async bet writer ───────────────────────────────────
def test_writer_default_async_and_mode_mutation():
    assert sh.state.db_write_mode == "async"
    sh._apply_mutation({"db_write_mode": "SYNC"})
    assert sh.state.db_write_mode == "sync"
    with pytest.raises(ValueError):
        sh._apply_mutation({"db_write_mode": "fast"})


def _result(i=0):
    return {"amount": B, "result_value": 2.5 + i, "result_display": "2.50x", "is_win": True}


def test_writer_batches_and_drains(cli_state, writer_reset):
    cli_state()
    sh._start_bet_writer()
    for i in range(200):
        sh._db_save_bet_dispatch(_result(i), B, 10.0)
    sh._stop_bet_writer()
    assert _bet_count(sh.DB_PATH) == 200
    st = sh._bet_writer_stats
    assert st["flushed"] == 200 and 1 <= st["batches"] < 200 and st["fallback_sync"] == 0


def test_writer_queue_full_falls_back_to_sync(cli_state, writer_reset, monkeypatch):
    cli_state()
    monkeypatch.setattr(sh, "_bet_write_queue", _queue.Queue(maxsize=1))
    sh._bet_write_queue.put_nowait(("filler",))
    sh._db_save_bet_async(_result(), B, 10.0)
    assert sh._bet_writer_stats["fallback_sync"] == 1
    assert _bet_count(sh.DB_PATH) == 1
    sh._bet_write_queue.get_nowait()


def test_dispatch_routes_by_mode(cli_state, writer_reset, monkeypatch):
    cli_state()
    calls = []
    monkeypatch.setattr(sh, "_db_save_bet", lambda *a: calls.append("sync"))
    monkeypatch.setattr(sh, "_db_save_bet_async", lambda *a: calls.append("async"))
    sh._db_save_bet_dispatch(_result(), B, 1)          # writer not started
    sh._start_bet_writer()
    sh._db_save_bet_dispatch(_result(), B, 1)
    sh.state.db_write_mode = "sync"
    sh._db_save_bet_dispatch(_result(), B, 1)
    assert calls == ["sync", "async", "sync"]


def test_bet_row_shape_matches_schema(cli_state):
    cli_state(game="dice")
    row = sh._make_bet_row(_result(), B, 9.5)
    assert len(row) == 10
    assert row[2] == "dice" and row[4] == 2.0 and row[7] == "win" and row[9] == 9.5


def test_loop_rows_land_through_async_writer(fake_casino, cli_state, run_cli, writer_reset):
    fake_casino.queue("win", "loss", "win", "loss")
    cli_state(max_bets=4)
    sh._start_bet_writer()
    run_cli()
    sh._stop_bet_writer()
    assert _bet_count(sh.DB_PATH) == 4
    assert sh._bet_writer_stats["queued"] == 4


def test_main_drains_writer_on_every_exit_path():
    src = inspect.getsource(sh.main)
    assert src.count("_stop_bet_writer()") == 3          # signal, daemon, TUI
    assert "_start_bet_writer()" in src and "_retention_worker" in src
    assert "_startup_purge()" in src


# ── 6a. presets + config keys ─────────────────────────────
def test_new_keys_in_every_list():
    for k in ("proxy", "purge_days", "db_write_mode"):
        assert k in sh._CONFIG_KEYS and k in sh._PRESET_KEYS
        assert k in sh._LIVE_RELOAD_KEYS and k in sh._TWEAK_PARAMS


def test_preset_round_trip_with_streak_tuples():
    s = sh.state
    s.streak_delay_loss, s.streakbet_loss = (3, 0.5), (0, 0.0)
    s.recurring, s.recurring_delay, s.purge_days, s.db_write_mode = True, 30, 9, "sync"
    sh.save_preset("p1")
    with open(sh.PRESET_PATH) as f:
        raw = json.load(f)["p1"]
    assert raw["streak_delay_loss"] == "3:0.5" and raw["streakbet_loss"] is None
    assert raw["recurring"] is True
    sh.state = sh.BotState()
    assert sh.load_preset("p1")
    assert sh.state.streak_delay_loss == (3, 0.5) and sh.state.streakbet_loss == (0, 0.0)
    assert sh.state.purge_days == 9 and sh.state.db_write_mode == "sync"


def test_sighup_reload_applies_proxy_and_purge(monkeypatch):
    class S:
        proxies = {}
    monkeypatch.setattr(sh, "_http", S())
    _save_min_config()
    with open(sh.CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.update(proxy="socks5://h:1080", purge_days=12)
    with open(sh.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    applied = sh._reload_live_config()
    assert {"proxy", "purge_days"} <= set(applied)
    assert sh._http.proxies["https"] == "socks5://h:1080" and sh.state.purge_days == 12


# ── 6b. _apply_mutation ───────────────────────────────────
def test_mutation_stop_conditions_and_off():
    sh._apply_mutation({"max_profit": "0.5", "max_bets": "100"})
    assert sh.state.max_profit == 0.5 and sh.state.max_bets == 100
    sh._apply_mutation({"max_profit": "off", "max_bets": "none"})
    assert sh.state.max_profit is None and sh.state.max_bets is None
    with pytest.raises(ValueError):
        sh._apply_mutation({"max_loss": "abc"})


def test_mutation_streak_tuple_and_milestones():
    sh._apply_mutation({"streak_delay_loss": "5:1.5", "milestone_wins": "10"})
    assert sh.state.streak_delay_loss == (5, 1.5) and sh.state.milestone_wins == 10
    sh._apply_mutation({"streak_delay_loss": "off", "milestone_wins": "off"})
    assert sh.state.streak_delay_loss == (0, 0.0) and sh.state.milestone_wins == 0


def test_mutation_profit_threshold_resets_next_milestone():
    sh.state.next_profit_milestone = 99
    sh._apply_mutation({"profit_threshold": "0.25"})
    assert sh.state.next_profit_milestone == 0.25


@pytest.mark.parametrize("v,expected", [("on", True), ("yes", True), ("1", True),
                                        ("off", False), ("no", False), ("0", False)])
def test_mutation_recurring_toggle(v, expected):
    sh._apply_mutation({"recurring": v})
    assert sh.state.recurring is expected


def test_mutation_delays_clamp_with_warning():
    msgs = sh._apply_mutation({"recurring_delay": "999999", "streak_reset_delay": "-5"})
    assert sh.state.recurring_delay == sh.RECURRING_DELAY_MAX and sh.state.streak_reset_delay == 0
    assert sum(m.startswith("⚠") for m in msgs) == 2


def test_mutation_streak_reset_losses():
    sh._apply_mutation({"streak_reset_losses": "20"})
    assert sh.state.streak_reset_losses == 20
    sh._apply_mutation({"streak_reset_losses": "off"})
    assert sh.state.streak_reset_losses == 0
    with pytest.raises(ValueError):
        sh._apply_mutation({"streak_reset_losses": "-1"})


def test_mutation_base_bet_sets_current_and_min():
    sh._apply_mutation({"base_bet": "0.01"})
    assert sh.state.base_bet == sh.state.current_bet == 0.01
    msgs = sh._apply_mutation({"base_bet": "0.0000001"})
    assert sh.state.base_bet == sh.MIN_BET and any("minimum" in m for m in msgs)


def test_mutation_multiplier_rederives_dice_target():
    sh.state.game, sh.state.dice_condition = "dice", "above"
    sh._apply_mutation({"multiplier_target": "3"})
    assert sh.state.multiplier_target == 3.0 and sh.state.dice_target == 67.0
    sh.state.game = "limbo"
    sh._apply_mutation({"multiplier_target": "5"})
    assert sh.state.dice_target == 67.0             # limbo leaves the dice target alone
    with pytest.raises(ValueError):
        sh._apply_mutation({"multiplier_target": "1.0"})


def test_tweak_aliases_resolve_to_params():
    for alias, canon in sh._TWEAK_ALIAS.items():
        assert canon in sh._TWEAK_PARAMS, alias
    assert sh._TWEAK_ALIAS["delay"] == "bet_delay"
    assert sh._TWEAK_ALIAS["multiplier"] == "multiplier_target"
    assert "pipeline" not in sh._TWEAK_ALIAS


def test_show_tweak_applies_and_restores_pause(monkeypatch):
    answers = iter(["bogus 1", "delay", "delay 0.25"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    sh.state.paused = False
    with sh.console.capture() as cap:
        sh.show_tweak()
    out = _plain(cap.get())
    assert sh.state.bet_delay == 0.25 and sh.state.paused is False
    assert "Unknown param" in out and "Missing value" in out and "Delay: 0.250s" in out


# ── 6c. risky config ──────────────────────────────────────
def _risky(**kw):
    s = sh.state
    s.current_balance, s.base_bet, s.max_loss, s.max_profit, s.max_bets = 100.0, 0.01, 1.0, 1.0, 100
    s.strategy_key, s.loss_mult, s.multiplier_target = "1", 2.0, 2.0
    s.streak_reset_losses, s.recurring, s.basebet_pct, s.proxy = 0, False, 0.0, None
    for k, v in kw.items():
        setattr(s, k, v)
    return sh._check_risky_config()


def test_risky_clean_config_is_empty():
    assert _risky() == []


def test_risky_each_rule():
    assert any("Aggressive Martingale" in w for w in _risky(strategy_key="2", loss_mult=2.5))
    assert not any("Aggressive" in w for w in _risky(strategy_key="2", loss_mult=2.0))
    for sk in ("2", "6"):                                    # 6 = Delay Martingale here
        assert any("No `max_loss`" in w for w in _risky(strategy_key=sk, max_loss=None))
    assert not any("max_loss" in w for w in _risky(strategy_key="1", max_loss=None))
    assert any("Anti-Martingale" in w for w in _risky(strategy_key="3", max_profit=None))
    assert any("Base bet is" in w for w in _risky(base_bet=5.0))
    assert any("Multiplier 20.0x" in w for w in _risky(multiplier_target=20.0))
    assert any("Recurring restart" in w
               for w in _risky(recurring=True, max_profit=None, max_bets=None))
    assert any("Strategy 7" in w for w in _risky(strategy_key="7", max_bets=None))
    assert any("basebet_pct" in w for w in _risky(basebet_pct=0.05))


def test_risky_chain_depth():
    ws = _risky(strategy_key="2", max_loss=None, streak_reset_losses=10,
                current_balance=0.511, base_bet=0.001)
    assert any("covers a 9-loss chain" in w and "Loss 10 in a row" in w for w in ws)
    assert not any("chain" in w for w in _risky(strategy_key="2", max_loss=None))


def test_wizard_has_step_5b_and_confirm_warns():
    src = inspect.getsource(sh.setup_wizard)
    assert "Step 5b / 7" in src and "step_advanced_tuning" in src
    assert "_check_risky_config()" in src


# ── 6d. config CLI ────────────────────────────────────────
def test_parse_config_set_pairs():
    assert sh._parse_config_set_pairs(["MaxProfit=0.5", "delay=0.1"]) == \
        {"max_profit": "0.5", "bet_delay": "0.1"}
    for bad in (["noequals"], ["nosuch=1"]):
        with pytest.raises(ValueError):
            sh._parse_config_set_pairs(bad)


def test_fmt_config_value():
    assert "off" in sh._fmt_config_value("max_profit", None)
    assert "on" in sh._fmt_config_value("recurring", True)
    assert sh._fmt_config_value("streak_delay_loss", (3, 0.5)) == "3:0.5"
    assert "off" in sh._fmt_config_value("streak_delay_loss", (0, 0.0))
    assert sh._fmt_config_value("base_bet", 0.001) == "0.00100000"
    assert "secret" not in sh._fmt_config_value("proxy", "http://u:secret@h:1")


def test_config_show_masks_token_and_proxy():
    _save_min_config(proxy="http://user:secret@h:8080")
    out = _run(sh.cmd_config_show)
    assert "tok-abcd...wxyz" in out and "(SET)" in out
    assert "secret" not in out and "abcdefghijklmnop" not in out
    assert "purge_days" in out and "db_write_mode" in out


def test_config_show_without_config_exits():
    with pytest.raises(SystemExit) as ex:
        _run(sh.cmd_config_show)
    assert ex.value.code == 1


def test_config_set_writes_disk_and_signals_daemon(monkeypatch):
    _save_min_config()
    with open(sh.PID_PATH, "w") as f:
        f.write("4242")
    kills = []
    monkeypatch.setattr(sh.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    out = _run(sh.cmd_config_set, ["maxprofit=0.5", "purgedays=99", "dbmode=sync"])
    with open(sh.CONFIG_PATH) as f:
        cfg = json.load(f)
    assert cfg["max_profit"] == 0.5 and cfg["purge_days"] == 30 and cfg["db_write_mode"] == "sync"
    assert kills == [(4242, signal.SIGHUP)]
    assert "⚠ purge_days clamped" in out and "Daemon reloaded live config" in out
    assert "need a full restart" in out and "db_write_mode" in out


@pytest.mark.parametrize("pairs", [["bearer_token=x"], ["nosuch=1"], ["noequals"], ["maxloss=abc"]])
def test_config_set_refusals_exit_2(pairs):
    _save_min_config()
    with pytest.raises(SystemExit) as ex:
        _run(sh.cmd_config_set, pairs)
    assert ex.value.code == 2


def test_config_set_without_config_exits_1():
    with pytest.raises(SystemExit) as ex:
        _run(sh.cmd_config_set, ["maxprofit=1"])
    assert ex.value.code == 1


def test_argparse_version_and_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--version"])
    with pytest.raises(SystemExit):
        sh.main()
    assert f"Shuffle AutoBot v{sh.VERSION}" in capsys.readouterr().out
    calls = []
    for flag, fn in [("--config-show", "cmd_config_show"), ("--diagnose", "cmd_diagnose")]:
        monkeypatch.setattr(sh, fn, lambda *a, _f=fn: calls.append((_f, a)))
        monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", flag])
        sh.main()
    monkeypatch.setattr(sh, "cmd_benchmark", lambda n: calls.append(("bench", n)))
    monkeypatch.setattr(sh, "cmd_cleanup", lambda d: calls.append(("clean", d)))
    monkeypatch.setattr(sh, "cmd_config_set", lambda p: calls.append(("set", p)))
    for argv in (["--benchmark"], ["--benchmark", "7"], ["--cleanup"], ["--cleanup", "4"],
                 ["--config-set", "a=1", "b=2"]):
        monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", *argv])
        sh.main()
    assert calls == [("cmd_config_show", ()), ("cmd_diagnose", ()), ("bench", 20), ("bench", 7),
                     ("clean", None), ("clean", 4), ("set", ["a=1", "b=2"])]


def test_help_epilog_lists_examples(monkeypatch, capsys):
    monkeypatch.setattr(sh.sys, "argv", ["shuffle.py", "--help"])
    with pytest.raises(SystemExit):
        sh.main()
    out = capsys.readouterr().out
    assert "--config-set maxprofit=0.5" in out and "--benchmark 20" in out


# ── 6e. monitor UX ────────────────────────────────────────
def test_monitor_footer_hints():
    d = {"running": True, "status": "x", "game": "limbo", "currency": "usdt"}
    screen = _plain(sh._build_monitor_screen(d, [], True))
    assert "[S]top daemon" in screen and "--config-set KEY=VALUE" in screen


def test_is_systemd_managed(monkeypatch, tmp_path):
    assert sh._is_systemd_managed(None) is False
    real_open = open
    cg = tmp_path / "cg"

    def fake_open(path, *a, **k):
        if str(path) == "/proc/77/cgroup":
            return real_open(cg, *a, **k)
        if str(path).startswith("/proc/"):
            raise OSError("missing")
        return real_open(path, *a, **k)

    cg.write_text("0::/user.slice/app.slice/shuffle.service\n")
    monkeypatch.setattr("builtins.open", fake_open)
    assert sh._is_systemd_managed(77) is True
    assert sh._is_systemd_managed(78) is False
    cg.write_text("0::/user.slice/app.slice/shuffle-tg.service\n")
    assert sh._is_systemd_managed(77) is False


def test_monitor_stop_goes_through_systemctl():
    src = inspect.getsource(sh.cmd_monitor)
    assert "systemctl" in src and "_is_systemd_managed(pid)" in src
