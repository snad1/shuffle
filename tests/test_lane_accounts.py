"""Shuffle's token in the host vault, and lanes that bet from their own account.

Reference: wolfbet/tests/test_phase15_vault.py and goated/tests/test_lane_accounts.py. The vault lives in each test's tmp dir
(conftest points CASINO_VAULT_DIR and CASINO_VAULT_KEY_FILE there).
"""

import io
import json
import logging
import os
import signal
import sys
from pathlib import Path

import pytest

import shuffle as go
from shared.core.vault import Vault

TOKEN = "shuffle-main-token-0123456789abcdef"
FRIEND = "shuffle-friend-token-fedcba9876543210"
PROXY = "socks5://friend:pw-secret@10.0.0.9:1080"


@pytest.fixture(autouse=True)
def _lane_paths(monkeypatch):
    monkeypatch.setattr(go, "_BASE_STATE_PATH", go.STATE_PATH)
    monkeypatch.setattr(go, "_BASE_PID_PATH", go.PID_PATH)
    monkeypatch.setattr(go, "_log_tag", go._log_tag)
    fmt = go._log_handler.formatter
    yield
    go._log_handler.setFormatter(fmt)


def _vault():
    return Vault("shuffle")


def _write(data: dict) -> Path:
    path = Path(go.CONFIG_PATH)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


def _raw() -> dict:
    return json.loads(Path(go.CONFIG_PATH).read_text())


def _base(**extra) -> dict:
    return {"bearer_token": TOKEN, "currency": "usdt",
            "game": "limbo", "base_bet": 0.01, "multiplier_target": 2.0, **extra}


def _load(lane=1):
    go.state = go.BotState()
    go.state.lane = lane
    assert go.load_config()
    return go.state


def _items(**lanes) -> dict:
    return {"split": "budget", "next_id": max(int(k) for k in lanes) + 1,
            "items": {k: {"name": f"Lane {k}", "enabled": True, "overrides": v}
                      for k, v in lanes.items()}}


def _set(pairs) -> int:
    """shuffle's config-set exits on a refusal and returns on success: give one exit code."""
    try:
        go.cmd_config_set(pairs)
    except SystemExit as e:
        return e.code
    return 0


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text + "\n"))


# ── the main token moves into the vault ─────────────────────────────────

def test_lane_one_moves_the_token_in_and_keeps_one_private_backup(monkeypatch):
    path = _write(_base())
    old = path.read_text()
    _load()
    go._sync_vault_token()
    assert _vault().secret("default") == TOKEN
    assert "bearer_token" not in _raw()
    backup = Path(go.CONFIG_PATH + ".pre-vault")
    assert backup.read_text() == old and oct(backup.stat().st_mode & 0o777) == "0o600"
    assert _load().bearer_token == TOKEN          # vault only from now on
    go.save_config()
    assert "bearer_token" not in _raw()


def test_a_broken_vault_keeps_the_config_copy(caplog):
    _write(_base())
    kek = Path(os.environ["CASINO_VAULT_KEY_FILE"])
    kek.parent.mkdir(parents=True)
    kek.write_bytes(b"x" * 32)
    os.chmod(kek, 0o644)
    with caplog.at_level(logging.WARNING, logger="shuffle"):
        s = _load()
        go._sync_vault_token()
    assert s.bearer_token == TOKEN and _raw()["bearer_token"] == TOKEN
    assert "Key vault unavailable" in caplog.text and TOKEN not in caplog.text
    assert not Path(go.CONFIG_PATH + ".pre-vault").exists()


def test_lanes_two_and_up_never_write_the_vault():
    _write(_base(lanes=_items(**{"1": {}, "2": {}})))
    s = _load(2)
    go._sync_vault_token()
    assert s.bearer_token == TOKEN and _vault().info("default") is None
    assert _raw()["bearer_token"] == TOKEN


# ── a lane on its own account ───────────────────────────────────────────

def _friend_vault():
    v = _vault()
    v.put("default", TOKEN, "main")
    v.put("friend-a", FRIEND, "Friend A")
    v.put("friend-px", PROXY, "Friend proxy", kind="proxy")


def test_a_lane_bets_from_its_own_token_currency_and_proxy():
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "3": {"key_ref": "friend-a", "currency": "trx",
                                              "proxy_ref": "friend-px"}})))
    s = _load(3)
    assert (s.bearer_token, s.currency, s.proxy) == (FRIEND, "trx", PROXY)
    assert s.account_problem is None and s.split_lanes == 1
    one = _load(1)
    assert (one.bearer_token, one.currency, one.proxy) == (TOKEN, "usdt", None)


def test_split_counts_only_lanes_on_the_same_account():
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "2": {}, "3": {"key_ref": "friend-a"}})))
    assert _load(1).split_lanes == 2 and _load(3).split_lanes == 1


def test_a_lane_whose_key_is_gone_stops_instead_of_using_another_account(capsys):
    _vault().put("default", TOKEN, "main")
    _write(_base(lanes=_items(**{"1": {}, "3": {"key_ref": "friend-a"}})))
    s = _load(3)
    assert s.bearer_token == "" and s.account_problem == "Lane 3: key friend-a is not in the vault"
    with pytest.raises(SystemExit) as exc:
        go._load_and_connect()
    assert exc.value.code == 1
    assert "Lane 3: key friend-a is not in the vault" in capsys.readouterr().out


def test_a_missing_vault_proxy_is_a_problem_too():
    _write(_base(lanes=_items(**{"1": {}, "2": {"proxy_ref": "gone"}})))
    s = _load(2)
    assert s.proxy is None and s.account_problem == "Lane 2: proxy gone is not in the vault"


def test_the_vault_proxy_survives_a_reload_and_never_reaches_the_file():
    _friend_vault()
    _write(_base(proxy_ref="friend-px", lanes=_items(**{"1": {}})))
    s = _load(1)
    assert s.proxy == PROXY
    go._reload_live_config()
    assert s.proxy == PROXY
    go.save_config()
    assert "pw-secret" not in Path(go.CONFIG_PATH).read_text()


# ── config-set refusals and restarts ────────────────────────────────────

@pytest.fixture
def wide(monkeypatch):
    monkeypatch.setattr(go.console, "width", 200)


@pytest.mark.parametrize(("pair", "message"), [
    ("lane3.key_ref=nobody", "Invalid lane3 override: no key named nobody in the vault"),
    ("lane3.proxy_ref=friend-a", "Invalid lane3 override: no proxy named friend-a in the vault"),
    (f"lane3.proxy={PROXY}", "a proxy with a password goes in the vault"),
    ("lane3.currency=zzz", "currency must be one of"),
])
def test_bad_lane_accounts_are_refused_without_echo(wide, capsys, pair, message):
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "3": {}})))
    before = Path(go.CONFIG_PATH).read_text()
    assert _set([pair]) == 2
    out = capsys.readouterr().out
    assert message in out and "pw-secret" not in out
    assert Path(go.CONFIG_PATH).read_text() == before


@pytest.mark.parametrize("pair", [f"bearer_token={FRIEND}", f"lane3.bearer_token={FRIEND}",
                                  f"lane2.token={FRIEND}"])
def test_credentials_never_go_through_config_set(wide, capsys, pair):
    _write(_base(lanes=_items(**{"1": {}, "3": {}})))
    assert _set([pair]) == 2
    out = capsys.readouterr().out
    assert "Refusing to write bearer_token" in out and FRIEND not in out


def test_only_the_lane_whose_account_changed_restarts(wide, monkeypatch, capsys):
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "2": {}, "3": {}})))
    for lane in (1, 2, 3):
        Path(go.lane_path(go.PID_PATH, lane)).write_text(str(100 + lane))
    monkeypatch.setattr(go.os, "kill", lambda pid, sig: None)
    restarted = []
    monkeypatch.setattr(go, "_restart_lane", lambda lane: restarted.append(lane) or True)
    assert _set(["lane3.key_ref=friend-a", "lane3.currency=trx"]) == 0
    assert restarted == [3]
    assert "Lane 3 restarted: its key, currency or proxy changed." in capsys.readouterr().out
    assert _set(["lane2.maxloss=1"]) == 0
    assert restarted == [3]


# ── key verbs ───────────────────────────────────────────────────────────

def test_a_key_is_added_from_stdin_and_listed_with_its_lanes(wide, monkeypatch, capsys):
    _write(_base(lanes=_items(**{"1": {}, "2": {"key_ref": "friend-a"}})))
    _stdin(monkeypatch, FRIEND)
    assert go.cmd_key("put", "friend-a", "Friend A") == 0
    out = capsys.readouterr().out
    assert "Key friend-a added (ends 3210)." in out and FRIEND not in out
    assert _vault().secret("friend-a") == FRIEND
    assert go.cmd_key("list") == 0
    out = capsys.readouterr().out
    assert "friend-a" in out and "Friend A" in out and "3210" in out and FRIEND not in out


def test_rotate_replaces_the_secret_and_says_when_it_applies(monkeypatch, capsys):
    _vault().put("friend-a", FRIEND, "Friend A")
    _write(_base())
    _stdin(monkeypatch, "shuffle-friend-token-new-000000009999")
    assert go.cmd_key("rotate", "friend-a") == 0
    out = capsys.readouterr().out
    assert "Key friend-a rotated (ends 9999)." in out and "when they restart" in out
    assert _vault().info("friend-a").label == "Friend A"


@pytest.mark.parametrize(("action", "key_id", "kind", "text", "message"), [
    ("put", "Bad Id", "key", FRIEND, "key id must be 1-32 lowercase"),
    ("put", "friend-a", "key", FRIEND, "A key named friend-a already exists."),
    ("rotate", "nobody", "key", FRIEND, "No key named nobody."),
    ("delete", "default", "key", "", "This key is used by lane 1, 2. Move the lanes to another key first."),
    ("put", "bot", "telegram", "12345:" + "a" * 35, "The Telegram bot token is stored as telegram"),
    ("put", "telegram", "telegram", "not-a-telegram-token", "doesn't look like a Telegram bot token"),
    ("put", "short", "key", "abc", "the key must be 8-4096 printable characters"),
    ("put", "px", "proxy", "ftp://user:pw@host:1", "a proxy must be an http://"),
])
def test_key_refusals_exit_2_and_never_echo(wide, monkeypatch, capsys, action, key_id, kind, text,
                                            message):
    _vault().put("default", TOKEN, "main")
    _vault().put("friend-a", FRIEND, "Friend A")
    _write(_base(lanes=_items(**{"1": {}, "2": {}})))
    _stdin(monkeypatch, text)
    assert go.cmd_key(action, key_id, None, kind) == 2
    out = capsys.readouterr().out
    assert message in out
    assert not text or text not in out


def test_an_unused_key_is_deleted_and_telegram_has_its_own_note(monkeypatch, capsys):
    _vault().put("friend-a", FRIEND, "Friend A")
    _write(_base())
    assert go.cmd_key("delete", "friend-a") == 0
    assert "Key friend-a deleted." in capsys.readouterr().out and _vault().info("friend-a") is None
    _stdin(monkeypatch, "12345:" + "a" * 35)
    assert go.cmd_key("put", "telegram", None, "telegram") == 0
    assert "shufflectl tg restart" in capsys.readouterr().out


def test_key_flags_read_the_secret_from_stdin(monkeypatch, capsys):
    _write(_base())
    _stdin(monkeypatch, FRIEND)
    monkeypatch.setattr(sys, "argv", ["shuffle.py", "--key-put", "friend-a", "--key-label", "Friend"])
    with pytest.raises(SystemExit) as exc:
        go.main()
    assert exc.value.code == 0 and _vault().info("friend-a").label == "Friend"


# ── the Telegram bot token ──────────────────────────────────────────────

def test_the_telegram_bot_reads_its_token_from_the_vault_first(monkeypatch):
    from tg import config as tg_config
    monkeypatch.setenv("SHUFFLE_TG_TOKEN", "12345:env-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert tg_config.bot_token() == "12345:env-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _vault().put("telegram", "67890:" + "b" * 35, "bot", kind="telegram")
    assert tg_config.bot_token() == "67890:" + "b" * 35
