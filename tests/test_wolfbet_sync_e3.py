"""E3-S2 — shuffle CLI betting behaviour (WolfBet CLI v2.16.0 … v2.32.1).

Mirrors wolfbet tests test_phase5f_recurring_delay, test_phase5o_config_persistence,
test_phase5p_live_reload, test_phase5q_uptime_pause, test_phase5u_profit_increment_persistence,
test_phase6b_ms_window_display, test_phase9_streak_reset (CLI half) and
test_phase9b_insufficient_400 (terminal part), on the E0 fake casino.

Run: APP_ENV=testing .venv-test/bin/python -m pytest shuffle/tests -q
"""

import json
import re
import sqlite3

import pytest

import shuffle as sh

B = sh.MIN_BET  # flat bet at 2.0x: a win is +B, a loss is -B


def _sessions(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT id, ended_at, total_bets, wins, losses, profit FROM sessions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ── recurring cycles ──────────────────────────────────────
def test_recurring_opens_new_session_after_delay(fake_casino, cli_state, run_cli, sleeps):
    # cycle 1: 3 wins, cycle 2: 3 wins, cycle 3: script empty -> insufficient (terminal)
    fake_casino.queue(*["win"] * 6)
    cli_state(max_bets=3, recurring=True, recurring_delay=30)
    first_start = sh.state.session_start
    s = run_cli()

    rows = _sessions(sh.DB_PATH)
    assert len(rows) == 3
    assert [r[2] for r in rows] == [3, 3, 0]
    assert all(r[1] for r in rows)                 # every row closed
    assert sum(sleeps) == pytest.approx(60)        # two 30 s pauses
    assert s.status == "STOPPED: Insufficient balance"
    assert s.run_started_at <= first_start < s.session_start


def test_recurring_never_restarts_after_max_loss(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("loss", "loss", "win")
    cli_state(max_bets=None, max_loss=2 * B, recurring=True, recurring_delay=30)
    s = run_cli()

    assert s.status.startswith("STOPPED: Max loss hit")
    assert len(_sessions(sh.DB_PATH)) == 1
    assert sum(sleeps) == 0


def test_recurring_never_restarts_in_the_red(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("loss", "loss")
    cli_state(max_bets=2, recurring=True, recurring_delay=30)
    s = run_cli()

    assert s.status == "STOPPED: Max bets reached: 2"
    assert len(_sessions(sh.DB_PATH)) == 1


def test_quit_during_pause_opens_no_new_session(fake_casino, cli_state, run_cli, monkeypatch):
    fake_casino.queue("win", "win")
    cli_state(max_bets=2, recurring=True, recurring_delay=30)

    def quit_now(_sec):
        sh.state.running = False                   # what the `q` key does

    monkeypatch.setattr(sh.time, "sleep", quit_now)
    run_cli()
    assert len(_sessions(sh.DB_PATH)) == 1


def test_pause_freezes_uptime_and_reports_remaining(fake_casino, cli_state, run_cli, monkeypatch):
    fake_casino.queue("win", "win")
    cli_state(max_bets=2, recurring=True, recurring_delay=30)
    seen = {}

    def during_pause(_sec):
        if not seen:
            sh._save_state_file()
            with open(sh.STATE_PATH) as f:
                seen.update(json.load(f))
            seen["frozen"] = sh.state.session_bet_ended_at
        sh.state.running = False

    monkeypatch.setattr(sh.time, "sleep", during_pause)
    run_cli()
    assert seen["frozen"] is not None
    assert seen["pause_remaining_sec"] > 0
    assert seen["pause_total_sec"] == 30
    assert seen["recurring"] is True and seen["recurring_delay_sec"] == 30


def test_uptime_uses_frozen_bet_end(monkeypatch):
    s = sh.state
    s.session_start, s.session_bet_ended_at, s.run_started_at = 100.0, 160.0, 0.0
    monkeypatch.setattr(sh.time, "time", lambda: 1000.0)
    assert sh._uptime_sec() == 60
    s.session_bet_ended_at = None
    assert sh._uptime_sec() == 900
    sh._save_state_file()
    with open(sh.STATE_PATH) as f:
        assert json.load(f)["run_uptime_sec"] == 1000


def test_session_save_during_pause_keeps_frozen_end(cli_state):
    # periodic_save / quit run _db_save_session while the pause is on
    from datetime import datetime
    cli_state()
    frozen = datetime(2026, 1, 2, 3, 4, 5).timestamp()
    sh.state.session_bet_ended_at = frozen
    sh._db_save_session()
    assert _sessions(sh.DB_PATH)[0][1] == "2026-01-02T03:04:05"


# ── streak reset (v2.32.0) ────────────────────────────────
def test_streak_reset_restarts_with_recurring_off(fake_casino, cli_state, run_cli, sleeps):
    # +3B, then 2 losses (+1B), then the win that breaks them (+2B >= 0) -> reset
    fake_casino.queue("win", "win", "win", "loss", "loss", "win")
    cli_state(max_bets=None, streak_reset_losses=2, streak_reset_delay=600)
    s = run_cli()

    rows = _sessions(sh.DB_PATH)
    assert len(rows) == 2 and rows[0][2] == 6
    assert sum(sleeps) == pytest.approx(600)
    assert s.status == "STOPPED: Insufficient balance"   # cycle 2 ran the script dry


def test_streak_reset_not_in_the_red(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "win")
    cli_state(max_bets=None, streak_reset_losses=2, streak_reset_delay=600)
    s = run_cli()

    assert len(_sessions(sh.DB_PATH)) == 1
    assert s.total_bets == 3


def test_streak_reset_matrix():
    s = sh.state
    s.streak_reset_losses, s.profit = 3, 0.0
    s.current_streak, s.last_broken_loss_streak = 1, 3
    assert sh.should_stop() == (True, "Loss streak reset: 3 losses then win")
    s.current_streak = 2                           # second win disarms
    assert sh.should_stop() == (False, "")
    s.current_streak, s.last_broken_loss_streak = 1, 2   # streak too short
    assert sh.should_stop() == (False, "")
    s.streak_reset_losses, s.last_broken_loss_streak = 0, 9   # feature off
    assert sh.should_stop() == (False, "")


# ── profit_increment milestone (v2.30.1 / v2.30.7) ────────
def test_profit_milestone_rearmed_per_cycle_and_base_bump_kept(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "win", "win")
    cli_state(max_bets=3, recurring=True, recurring_delay=0,
              profit_increment=B, profit_threshold=2 * B, next_profit_milestone=2 * B)
    s = run_cli()

    # bumps at +2B (base 2B) and +4B (base 3B); the new cycle re-arms 2B
    assert s.base_bet == pytest.approx(3 * B)
    assert s.current_bet == pytest.approx(3 * B)
    assert s.next_profit_milestone == pytest.approx(2 * B)
    assert len(_sessions(sh.DB_PATH)) == 2
    sh._save_state_file()
    with open(sh.STATE_PATH) as f:
        assert json.load(f)["next_profit_milestone"] == pytest.approx(2 * B)


def test_cfg_row_ms_window_rec_and_sr():
    row = _plain(sh._build_config_progress_row({
        "profit": 0.003, "profit_threshold": 0.002, "profit_increment": 0.001,
        "next_profit_milestone": 0.004,
        "recurring": True, "recurring_delay_sec": 60,
        "streak_reset_losses": 3, "streak_reset_delay_sec": 600,
    }))
    assert "Ms +0.00100000/0.00200000 (50%)" in row
    assert "Rec on(60s)" in row
    assert "SR 3L→600s" in row


def test_cfg_row_from_botstate_uses_next_milestone():
    s = sh.state
    s.profit, s.profit_threshold, s.profit_increment = 0.001, 0.002, 0.001
    s.next_profit_milestone = 0.002
    s.recurring, s.recurring_delay = True, 45
    row = _plain(sh._build_config_progress_row(sh._state_dict_from_botstate()))
    assert "Ms +0.00100000/0.00200000 (50%)" in row
    assert "Rec on(45s)" in row


# ── loop hooks ────────────────────────────────────────────
def test_streak_delay_before_next_bet(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("loss", "loss", "win")
    cli_state(max_bets=3, streak_delay_loss=(2, 5.0))
    run_cli()
    assert sum(sleeps) == pytest.approx(5.0)


def test_streakbet_loss_scales_next_bet(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "win")
    cli_state(max_bets=3, strategy="Martingale", strategy_key="2", loss_mult=2.0,
              streakbet_loss=(2, 0.5))
    run_cli()
    assert [b["amount"] for b in fake_casino.bets] == pytest.approx([B, 2 * B, 2 * B])


def test_basebet_pct_every_ten_bets(fake_casino, cli_state, run_cli):
    fake_casino.queue(*["win"] * 11)
    cli_state(max_bets=11, basebet_pct=0.001)
    run_cli()
    bal_after_10 = fake_casino.bets[9]["balance"]
    assert fake_casino.bets[10]["amount"] == pytest.approx(round(bal_after_10 * 0.001, 8))


def test_milestones():
    s = sh.state
    s.milestone_bets, s.total_bets = 4, 8
    assert sh._check_milestone("loss") == "8 bets"
    s.milestone_bets, s.milestone_wins, s.wins = 0, 3, 6
    assert sh._check_milestone("win") == "6 wins"
    s.milestone_wins, s.milestone_profit, s.profit = 0, 0.5, 1.2
    assert sh._check_milestone("win") == "1.00000000 profit"
    assert sh._check_milestone("win") is None       # same multiple not repeated


# ── insufficient balance (v2.32.1) ────────────────────────
def test_insufficient_balance_never_restarts(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("win", "insufficient_balance")
    cli_state(max_bets=None, recurring=True, recurring_delay=30,
              streak_reset_losses=1, streak_reset_delay=600)
    s = run_cli()
    assert s.status == "STOPPED: Insufficient balance"
    assert len(_sessions(sh.DB_PATH)) == 1
    assert sum(sleeps) == 0


def test_balance_message_is_not_insufficient(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "api_error_balance", "win")
    cli_state(max_bets=2)
    s = run_cli()
    assert not s._insufficient_balance
    assert s.status == "STOPPED: Max bets reached: 2"


# ── persistence (v2.28.2) + clamps (v2.30.6) ──────────────
NEW_KEYS = {
    "recurring": True, "recurring_delay": 120, "basebet_pct": 0.002,
    "milestone_bets": 50, "milestone_wins": 7, "milestone_losses": 9, "milestone_profit": 0.5,
    "streak_reset_losses": 4, "streak_reset_delay": 900,
    "streak_delay_loss": (3, 1.5), "streak_delay_win": (2, 0.5),
    "streak_delay_bets": (100, 10.0), "streakbet_loss": (4, 0.25),
}


def _set_new_keys():
    for k, v in NEW_KEYS.items():
        setattr(sh.state, k, v)


def test_config_round_trip(monkeypatch):
    _set_new_keys()
    sh.save_config()
    with open(sh.CONFIG_PATH) as f:
        raw = json.load(f)
    assert raw["streak_delay_loss"] == "3:1.5"
    assert raw["streakbet_loss"] == "4:0.25"

    monkeypatch.setattr(sh, "state", sh.BotState())
    assert sh.load_config()
    for k, v in NEW_KEYS.items():
        assert getattr(sh.state, k) == v, k


def test_preset_round_trip(monkeypatch):
    _set_new_keys()
    sh.save_preset("p1")
    monkeypatch.setattr(sh, "state", sh.BotState())
    assert sh.load_preset("p1")
    for k, v in NEW_KEYS.items():
        assert getattr(sh.state, k) == v, k


def test_disabled_streak_tuple_saved_as_null():
    sh.save_config()
    with open(sh.CONFIG_PATH) as f:
        assert json.load(f)["streak_delay_loss"] is None


def test_load_clamps_delays_with_message(capsys):
    with open(sh.CONFIG_PATH, "w") as f:
        json.dump({"recurring_delay": 100000, "streak_reset_delay": -5}, f)
    assert sh.load_config()
    assert sh.state.recurring_delay == 86400
    assert sh.state.streak_reset_delay == 0
    out = capsys.readouterr().out
    assert "recurring_delay clamped from 100000 to 86400" in out
    assert "streak_reset_delay clamped" in out


# ── SIGHUP live reload (v2.28.3) ──────────────────────────
def test_sighup_reloads_live_keys_only():
    s = sh.state
    s.base_bet, s.currency, s.max_bets = B, "usdt", 10
    sh.save_config()
    with open(sh.CONFIG_PATH) as f:
        data = json.load(f)
    data.update({"max_bets": 50, "recurring": True, "streak_delay_loss": "3:2",
                 "recurring_delay": 999999, "base_bet": 5.0, "currency": "btc"})
    with open(sh.CONFIG_PATH, "w") as f:
        json.dump(data, f)
    s.session_id, s.total_bets, s.profit = 7, 4, 0.01

    sh._sig_reload(None, None)

    assert (s.max_bets, s.recurring, s.streak_delay_loss) == (50, True, (3, 2.0))
    assert s.recurring_delay == 86400                     # clamped on reload too
    assert (s.base_bet, s.currency) == (B, "usdt")        # not live-safe
    assert (s.session_id, s.total_bets, s.profit) == (7, 4, 0.01)


def test_main_registers_sighup():
    import inspect
    src = inspect.getsource(sh.main)
    assert "signal.SIGHUP, _sig_reload" in src
    assert "signal.SIGUSR1, _sig_pause" in src and "signal.SIGUSR2, _sig_resume" in src
