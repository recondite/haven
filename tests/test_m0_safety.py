"""M0 safety enforcement: audit immutability, panic switch precedence, boot
tripwire, stuck-send resolution, backups."""
import sqlite3

import pytest

from haven import backup, config, executor
from haven import spine as spine_mod
from haven.spine import Spine


@pytest.fixture
def sp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEND_MODE", "dry")
    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    monkeypatch.setattr(executor, "spine", s)
    return s


# ─── audit append-only (triggers) ────────────────────────
def test_audit_update_aborts(sp):
    sp.audit("test", "something_happened")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with sp._lock:
            sp._conn.execute("UPDATE audit SET action='tampered' WHERE id=1")


def test_audit_delete_aborts(sp):
    sp.audit("test", "something_happened")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with sp._lock:
            sp._conn.execute("DELETE FROM audit WHERE id=1")


def test_audit_insert_still_works(sp):
    sp.audit("test", "a")
    sp.audit("test", "b")
    with sp._lock:
        n = sp._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    assert n == 2


# ─── panic switch / send-mode precedence ─────────────────
def test_runtime_override_beats_env(sp, monkeypatch):
    monkeypatch.setattr(config, "SEND_MODE", "live")
    assert executor.is_dry_run() is False
    sp.set_runtime_config("send_mode", "dry", by="gt")
    assert executor.is_dry_run() is True          # override wins
    sp.set_runtime_config("send_mode", "live", by="gt")
    assert executor.is_dry_run() is False


def test_set_send_mode_audited_and_validated(sp):
    res = executor.set_send_mode("dry")
    assert res["dry_run"] is True
    with pytest.raises(executor.ExecutorError):
        executor.set_send_mode("banana")
    with sp._lock:
        rows = [r["action"] for r in sp._conn.execute("SELECT action FROM audit").fetchall()]
    assert "send_mode_changed" in rows


def test_manual_flip_clears_forced_reason(sp):
    sp.set_runtime_config("send_mode_forced_reason", "tripwire fired", by="boot-tripwire")
    executor.set_send_mode("dry")
    assert sp.get_runtime_config("send_mode_forced_reason") is None


# ─── boot tripwire ───────────────────────────────────────
def test_tripwire_forces_dry_on_unsafe_posture(sp, monkeypatch):
    monkeypatch.setattr(config, "SEND_MODE", "live")
    monkeypatch.setattr(config, "HAVEN_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "HAVEN_AUTH_TOKEN", None)
    reason = executor.enforce_boot_tripwire()
    assert reason and "blocked" in reason
    assert executor.is_dry_run() is True
    assert sp.get_runtime_config("send_mode_forced_reason")


def test_tripwire_allows_localhost_live(sp, monkeypatch):
    monkeypatch.setattr(config, "SEND_MODE", "live")
    monkeypatch.setattr(config, "HAVEN_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "HAVEN_AUTH_TOKEN", None)
    assert executor.enforce_boot_tripwire() is None
    assert executor.is_dry_run() is False


def test_tripwire_allows_authed_nonlocal_live(sp, monkeypatch):
    monkeypatch.setattr(config, "SEND_MODE", "live")
    monkeypatch.setattr(config, "HAVEN_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "HAVEN_AUTH_TOKEN", "tok")
    assert executor.enforce_boot_tripwire() is None


# ─── stuck-send resolution ───────────────────────────────
def _stuck_action(sp):
    job = sp.create_job("draft_reply_slack", "cli", "slack/C1:1")
    did = sp.create_draft(job, "slack", "C1:1", "hello")
    aid, _ = sp.record_action(did, "slack", "C1:1", "sending", None)
    return aid


def test_resolve_from_sending(sp):
    aid = _stuck_action(sp)
    res = sp.resolve_action(aid, "sent", "reply visible in thread")
    assert res["status"] == "sent"
    assert sp.list_actions("sending") == []
    with sp._lock:
        rows = [r["action"] for r in sp._conn.execute("SELECT action FROM audit").fetchall()]
    assert "action_resolved" in rows


def test_resolve_guards(sp):
    aid = _stuck_action(sp)
    with pytest.raises(ValueError):
        sp.resolve_action(aid, "dry_run", "bad status")
    sp.resolve_action(aid, "failed", "nothing arrived")
    with pytest.raises(ValueError):                 # not 'sending' anymore
        sp.resolve_action(aid, "sent", "double resolve")
    with pytest.raises(ValueError):
        sp.resolve_action(9999, "sent", "missing")


def test_list_stuck_actions_carries_draft_context(sp):
    _stuck_action(sp)
    stuck = sp.list_actions("sending")
    assert len(stuck) == 1 and stuck[0]["draft_kind"] == "slack"


# ─── backups ─────────────────────────────────────────────
def test_backup_creates_then_skips(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    db = state / "spine.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "_SOURCES", (db,))
    r1 = backup.backup_now()
    assert r1[0]["status"] == "created"
    r2 = backup.backup_now()
    assert r2[0]["status"] == "exists"              # idempotent per day
    # snapshot is a valid, readable database
    snap = backup.BACKUP_DIR / r1[0]["file"]
    c = sqlite3.connect(str(snap))
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    c.close()
    st = backup.backup_status()
    assert st["count"] == 1 and st["latest"] == r1[0]["file"]
