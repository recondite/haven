"""M4 trust panel: action-verb usage counts, agent feedback aggregation."""
import pytest

from haven import spine as spine_mod
from haven.spine import Spine


@pytest.fixture
def sp(tmp_path, monkeypatch):
    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    return s


def _approved_action(sp, kind, target, status, agent="draft_reply_slack", verdict="approved_clean"):
    job = sp.create_job(agent, "cli", f"{kind}/{target}")
    did = sp.create_draft(job, kind, target, "body")
    sp.set_draft_status(did, "approved")
    sp.record_feedback(did, verdict)
    sp.record_action(did, kind, target, status, {"ok": True})
    return did


def test_verb_counts_by_kind(sp):
    _approved_action(sp, "slack", "C1:1", "sent")
    _approved_action(sp, "slack", "C1:2", "dry_run")
    _approved_action(sp, "email", "m1", "sent")
    counts = sp.action_verb_counts(30)
    assert counts["slack"]["count"] == 2
    assert counts["slack"]["sent"] == 1 and counts["slack"]["dry_run"] == 1
    assert counts["email"]["count"] == 1
    assert "wiki" not in counts                    # never used -> absent (flagged unused upstream)


def test_feedback_by_agent(sp):
    _approved_action(sp, "slack", "C1:1", "sent", agent="draft_reply_slack", verdict="approved_clean")
    _approved_action(sp, "slack", "C1:2", "sent", agent="draft_reply_slack", verdict="edited")
    _approved_action(sp, "email", "m1", "sent", agent="draft_reply_email", verdict="rejected")
    by = {a["agent"]: a for a in sp.feedback_by_agent()}
    assert by["draft_reply_slack"]["approved_clean"] == 1
    assert by["draft_reply_slack"]["edited"] == 1
    assert by["draft_reply_email"]["rejected"] == 1


def test_verb_counts_window_excludes_old(sp):
    did = _approved_action(sp, "slack", "C1:1", "sent")
    # force the action's timestamp far into the past
    with sp._lock:
        sp._conn.execute("UPDATE action SET created_at = datetime('now','-60 days') WHERE draft_id=?", (did,))
        sp._conn.commit()
    assert sp.action_verb_counts(30) == {}          # outside the 30-day window
