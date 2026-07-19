"""Phase 1 safe foundation: job->draft->approve(dry-run)->action, idempotency,
and the no-action-without-approved-draft invariant. No external sends."""
import asyncio

import pytest

from haven import dispatch, executor
from haven import spine as spine_mod
from haven.spine import Spine


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sp(tmp_path, monkeypatch):
    """A temp spine wired into every module that holds the singleton."""
    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    monkeypatch.setattr(executor, "spine", s)
    monkeypatch.setattr(dispatch, "spine", s)
    return s


def _draft(sp, kind="slack", target="C1:1.2", payload="hi there"):
    job_id = sp.create_job("draft_reply_slack", "cli", "slack/C1:1.2")
    return sp.create_draft(job_id, kind, target, payload, [{"source": "slack"}])


def test_draft_lifecycle(sp):
    did = _draft(sp)
    assert [d["id"] for d in sp.list_drafts("pending")] == [did]


def test_approve_dry_run_creates_action_and_feedback(sp):
    did = _draft(sp)
    res = executor.approve(did)
    assert res["created"] is True and res["dry_run"] is True and res["status"] == "dry_run"
    assert sp.get_draft(did)["status"] == "approved"
    assert sp.get_action_for_draft(did)["status"] == "dry_run"
    assert sp.list_drafts("pending") == []  # left the queue


def test_approve_is_idempotent(sp):
    did = _draft(sp)
    a1 = executor.approve(did)
    a2 = executor.approve(did)          # double-click / restart mid-approve
    assert a1["created"] is True and a2["created"] is False
    assert a1["action_id"] == a2["action_id"]
    # exactly one action row for this draft
    with sp._lock:
        n = sp._conn.execute("SELECT COUNT(*) FROM action WHERE draft_id=?", (did,)).fetchone()[0]
    assert n == 1


def test_rejected_draft_cannot_be_approved(sp):
    did = _draft(sp)
    executor.reject(did, "not needed")
    assert sp.get_draft(did)["status"] == "rejected"
    with pytest.raises(executor.ExecutorError):
        executor.approve(did)


def test_no_action_without_approved_draft(sp):
    """Invariant: every action row points at an approved (or edited) draft."""
    _draft(sp)                          # pending, never approved
    did2 = _draft(sp)
    executor.approve(did2)
    with sp._lock:
        rows = sp._conn.execute(
            "SELECT d.status FROM action a JOIN draft d ON d.id = a.draft_id"
        ).fetchall()
    assert rows and all(r["status"] in ("approved", "edited") for r in rows)


def test_edit_then_approve_records_edited_feedback(sp):
    did = _draft(sp, payload="original text here")
    executor.edit(did, "completely different reply")
    executor.approve(did)
    with sp._lock:
        fb = sp._conn.execute(
            "SELECT verdict, edit_distance FROM feedback WHERE draft_id=?", (did,)
        ).fetchone()
    assert fb["verdict"] == "edited" and fb["edit_distance"] > 0
    # what was approved is the edited text; original preserved for the record
    d = sp.get_draft(did)
    assert d["payload"] == "completely different reply"
    assert d["original_payload"] == "original text here"


def test_unedited_approve_stays_clean(sp):
    did = _draft(sp)
    executor.approve(did)
    with sp._lock:
        fb = sp._conn.execute("SELECT verdict FROM feedback WHERE draft_id=?", (did,)).fetchone()
    assert fb["verdict"] == "approved_clean"


def test_edit_guards(sp):
    did = _draft(sp)
    with pytest.raises(executor.ExecutorError):
        executor.edit(did, "   ")                 # empty
    executor.approve(did)
    with pytest.raises(executor.ExecutorError):
        executor.edit(did, "too late")            # not pending anymore


def test_live_send_is_not_built(sp):
    with pytest.raises(NotImplementedError):
        executor._send_live("slack", "C1:1.2", "hi")


def test_run_agent_produces_draft(sp, monkeypatch):
    # Fake the cached item and the LLM so the agent runs offline.
    class FakeStore:
        def get_cached_payloads(self, source, ids):
            return {ids[0]: {"msg_id": "C1:1.2", "sender": "Ada", "snippet": "can you send Q3 numbers?"}}
    monkeypatch.setattr(dispatch, "cursor_store", FakeStore())

    async def fake_call(prompt, model=None, timeout=60.0):
        return "  Here are the Q3 numbers.  "
    monkeypatch.setattr(dispatch.runtime, "call", fake_call)

    res = run(dispatch.run_agent("slack", "C1:1.2"))
    draft = sp.get_draft(res["draft_id"])
    assert draft["kind"] == "slack"
    assert draft["payload"] == "Here are the Q3 numbers."   # stripped
    assert draft["status"] == "pending"
