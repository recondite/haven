"""Phase 1: job->draft->approve->action pipeline.

Dry-run by default (approve records, sends nothing). Live mode is tested with
mocked transports only — no test ever performs a real send."""
import asyncio
import inspect

import pytest

from haven import config, dispatch, executor
from haven import spine as spine_mod
from haven.spine import Spine


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sp(tmp_path, monkeypatch):
    """A temp spine wired into every module that holds the singleton.
    Pins SEND_MODE=dry so the machine's .env can never arm tests."""
    monkeypatch.setattr(config, "SEND_MODE", "dry")
    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    monkeypatch.setattr(executor, "spine", s)
    monkeypatch.setattr(dispatch, "spine", s)
    return s


@pytest.fixture
def live(monkeypatch):
    """Arm live mode with FAKE transports; returns the list of performed sends."""
    sends = []

    async def fake_slack(target, payload):
        sends.append(("slack", target, payload))
        return {"provider": "slack", "ts": "111.222"}

    async def fake_email(target, payload):
        sends.append(("email", target, payload))
        return {"provider": "gmail", "id": "abc123"}

    monkeypatch.setattr(config, "SEND_MODE", "live")
    monkeypatch.setattr(executor, "_TRANSPORTS", {"slack": fake_slack, "email": fake_email})
    return sends


def _draft(sp, kind="slack", target="C1:1.2", payload="hi there"):
    job_id = sp.create_job("draft_reply_slack", "cli", "slack/C1:1.2")
    return sp.create_draft(job_id, kind, target, payload, [{"source": "slack"}])


# ─── dry-run (default) ───────────────────────────────────
def test_draft_lifecycle(sp):
    did = _draft(sp)
    assert [d["id"] for d in sp.list_drafts("pending")] == [did]


def test_approve_dry_run_creates_action_and_feedback(sp):
    did = _draft(sp)
    res = run(executor.approve(did))
    assert res["created"] is True and res["dry_run"] is True and res["status"] == "dry_run"
    assert sp.get_draft(did)["status"] == "approved"
    assert sp.get_action_for_draft(did)["status"] == "dry_run"
    assert sp.list_drafts("pending") == []


def test_approve_is_idempotent(sp):
    did = _draft(sp)
    a1 = run(executor.approve(did))
    a2 = run(executor.approve(did))          # double-click / restart mid-approve
    assert a1["created"] is True and a2["created"] is False
    assert a1["action_id"] == a2["action_id"]
    with sp._lock:
        n = sp._conn.execute("SELECT COUNT(*) FROM action WHERE draft_id=?", (did,)).fetchone()[0]
    assert n == 1


def test_rejected_draft_cannot_be_approved(sp):
    did = _draft(sp)
    executor.reject(did, "not needed")
    with pytest.raises(executor.ExecutorError):
        run(executor.approve(did))


def test_no_action_without_approved_draft(sp):
    _draft(sp)                               # pending, never approved
    did2 = _draft(sp)
    run(executor.approve(did2))
    with sp._lock:
        rows = sp._conn.execute(
            "SELECT d.status FROM action a JOIN draft d ON d.id = a.draft_id"
        ).fetchall()
    assert rows and all(r["status"] in ("approved", "edited") for r in rows)


# ─── editing ─────────────────────────────────────────────
def test_edit_then_approve_records_edited_feedback(sp):
    did = _draft(sp, payload="original text here")
    executor.edit(did, "completely different reply")
    run(executor.approve(did))
    with sp._lock:
        fb = sp._conn.execute(
            "SELECT verdict, edit_distance FROM feedback WHERE draft_id=?", (did,)
        ).fetchone()
    assert fb["verdict"] == "edited" and fb["edit_distance"] > 0
    d = sp.get_draft(did)
    assert d["payload"] == "completely different reply"
    assert d["original_payload"] == "original text here"


def test_unedited_approve_stays_clean(sp):
    did = _draft(sp)
    run(executor.approve(did))
    with sp._lock:
        fb = sp._conn.execute("SELECT verdict FROM feedback WHERE draft_id=?", (did,)).fetchone()
    assert fb["verdict"] == "approved_clean"


def test_edit_guards(sp):
    did = _draft(sp)
    with pytest.raises(executor.ExecutorError):
        executor.edit(did, "   ")                 # empty
    run(executor.approve(did))
    with pytest.raises(executor.ExecutorError):
        executor.edit(did, "too late")            # not pending anymore


# ─── live mode (mocked transports) ───────────────────────
def test_live_approve_sends_once(sp, live):
    did = _draft(sp, kind="slack", target="C9:42.1", payload="the answer")
    res = run(executor.approve(did))
    assert res["status"] == "sent" and res["dry_run"] is False
    assert live == [("slack", "C9:42.1", "the answer")]
    assert sp.get_action_for_draft(did)["status"] == "sent"


def test_live_double_approve_sends_exactly_once(sp, live):
    did = _draft(sp)
    run(executor.approve(did))
    res2 = run(executor.approve(did))
    assert res2["created"] is False and res2["status"] == "sent"
    assert len(live) == 1                        # one send, ever


def test_live_email_uses_gmail_transport(sp, live):
    did = _draft(sp, kind="email", target="19f6df9b92c73ad1", payload="Re: numbers")
    run(executor.approve(did))
    assert live == [("email", "19f6df9b92c73ad1", "Re: numbers")]


def test_live_transport_failure_marks_failed_no_retry(sp, live, monkeypatch):
    async def boom(target, payload):
        raise RuntimeError("slack 500")
    monkeypatch.setitem(executor._TRANSPORTS, "slack", boom)
    did = _draft(sp)
    res = run(executor.approve(did))
    assert res["status"] == "failed" and "slack 500" in res["result"]["error"]
    # slot stays claimed: re-approve does NOT resend
    res2 = run(executor.approve(did))
    assert res2["created"] is False and res2["status"] == "failed"


def test_crash_mid_send_surfaces_needs_verify(sp, live):
    """A row stuck in 'sending' (process died between send and update) must
    never be resent — approve reports needs_verify instead."""
    did = _draft(sp)
    sp.record_action(did, "slack", "C1:1.2", "sending", None)   # simulate the crash
    res = run(executor.approve(did))
    assert res["created"] is False and res["needs_verify"] is True
    assert live == []                            # nothing sent


def test_transports_have_no_delete_verbs():
    """Ground rule #1: outbound verbs are post/send/wiki-write/drive-write only —
    all create-or-update, never delete."""
    assert set(executor._TRANSPORTS) == {"slack", "email", "wiki", "drive"}
    src = inspect.getsource(executor)
    for banned in (".delete(", "chat.delete", "batchDelete", "messages.trash", ".trash(", "unlink", "rmtree"):
        assert banned not in src, f"executor source contains banned verb {banned!r}"


# ─── wiki ingest schema gate ─────────────────────────────
_GOOD_WIKI = ("---\ntype: concept\ntags: [cpo]\ncreated: 2026-07-19\nupdated: 2026-07-19\n---\n\n"
              "# Co-packaged optics\n\nCPO integrates optics beside compute.\n")


def test_wiki_schema_gate_blocks_bad_drafts(sp):
    job = sp.create_job("ingest", "cli", "ingest")
    bad_fm = sp.create_draft(job, "wiki", "wiki/concepts/x.md", "# No frontmatter\n\nbody")
    with pytest.raises(executor.ExecutorError):
        run(executor.approve(bad_fm))
    bad_target = sp.create_draft(job, "wiki", "notwiki/x.md", _GOOD_WIKI)
    with pytest.raises(executor.ExecutorError):
        run(executor.approve(bad_target))


def test_wiki_ingest_writes_on_approve(sp, live, tmp_path, monkeypatch):
    from haven import config
    from haven import executor as ex
    # point SecondBrain at a temp dir with a wiki/ + log.md + index.md
    sb = tmp_path / "SecondBrain"
    (sb / "wiki").mkdir(parents=True)
    (sb / "wiki" / "log.md").write_text("# Log\n", encoding="utf-8")
    (sb / "wiki" / "index.md").write_text(
        "# Index\n\n## People\n- [[someone]] — x (1 source)\n", encoding="utf-8")
    monkeypatch.setattr(config, "SECONDBRAIN_DIR", sb)
    # live fixture patched _TRANSPORTS to fakes; restore the real wiki writer
    monkeypatch.setitem(ex._TRANSPORTS, "wiki", ex._wiki_write)

    job = sp.create_job("ingest", "cli", "ingest")
    did = sp.create_draft(job, "wiki", "wiki/concepts/cpo.md", _GOOD_WIKI)
    res = run(ex.approve(did))
    assert res["status"] == "sent"
    written = (sb / "wiki" / "concepts" / "cpo.md").read_text(encoding="utf-8")
    assert "# Co-packaged optics" in written
    assert "Haven ingest" in (sb / "wiki" / "log.md").read_text(encoding="utf-8")
    # M3: catalogued under the dedicated append-only section; existing sections untouched
    idx = (sb / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "## Haven ingests (uncatalogued)" in idx
    assert "- [[cpo]] — ingested by Haven" in idx
    assert idx.startswith("# Index\n\n## People\n- [[someone]]")
    # second identical ingest must refuse (page now exists — never overwrite)
    did2 = sp.create_draft(job, "wiki", "wiki/concepts/cpo.md", _GOOD_WIKI)
    with pytest.raises(executor.ExecutorError):
        run(ex.approve(did2))


# ─── drive write (M-drive / SIM-176) ─────────────────────
class _FakeDrive:
    """Minimal Drive service double recording create/update calls."""
    def __init__(self):
        self.calls = []
    def files(self):
        return self
    def create(self, body=None, media_body=None, fields=None):
        self.calls.append(("create", body["name"]))
        return _FakeExec({"id": "newid1", "name": body["name"], "webViewLink": "http://doc/newid1"})
    def update(self, fileId=None, media_body=None, fields=None):
        self.calls.append(("update", fileId))
        return _FakeExec({"id": fileId, "name": "edited", "webViewLink": "http://doc/" + fileId})


class _FakeExec:
    def __init__(self, r):
        self._r = r
    def execute(self):
        return self._r


def _wire_drive(sp, live, monkeypatch):
    fake = _FakeDrive()
    class FakeAuth:
        async def get_drive_service(self):
            return fake
    import haven.deps
    monkeypatch.setattr(haven.deps, "gmail_auth", FakeAuth())
    monkeypatch.setitem(executor._TRANSPORTS, "drive", executor._drive_write)
    return fake


def test_drive_create_on_approve(sp, live, monkeypatch):
    fake = _wire_drive(sp, live, monkeypatch)
    job = sp.create_job("export", "cli", "export")
    did = sp.create_draft(job, "drive", "new:Q3 Memo", "the doc body")
    res = run(executor.approve(did))
    assert res["status"] == "sent"
    assert fake.calls == [("create", "Q3 Memo")]
    assert sp.get_action_for_draft(did)["status"] == "sent"


def test_drive_edit_existing_app_doc(sp, live, monkeypatch):
    fake = _wire_drive(sp, live, monkeypatch)
    job = sp.create_job("export", "cli", "export")
    did = sp.create_draft(job, "drive", "file:existing99", "new body")
    run(executor.approve(did))
    assert fake.calls == [("update", "existing99")]


def test_drive_unauthorized_marks_failed(sp, live, monkeypatch):
    class FakeAuth:
        async def get_drive_service(self):
            return None
    import haven.deps
    monkeypatch.setattr(haven.deps, "gmail_auth", FakeAuth())
    monkeypatch.setitem(executor._TRANSPORTS, "drive", executor._drive_write)
    job = sp.create_job("export", "cli", "export")
    did = sp.create_draft(job, "drive", "new:X", "body")
    res = run(executor.approve(did))
    assert res["status"] == "failed" and "not authorized" in res["result"]["error"]


# ─── agent dispatch ──────────────────────────────────────
def test_run_agent_produces_draft(sp, monkeypatch):
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
    assert draft["payload"] == "Here are the Q3 numbers."
    assert draft["status"] == "pending"


def test_run_agent_email(sp, monkeypatch):
    class FakeStore:
        def get_cached_payloads(self, source, ids):
            return {ids[0]: {"msg_id": "abc19f", "sender": "cfo@ayarlabs.com",
                             "subject": "Capex slide", "snippet": "need your number"}}
    monkeypatch.setattr(dispatch, "cursor_store", FakeStore())

    async def fake_call(prompt, model=None, timeout=60.0):
        return "2.41M. Thanks, GT"
    monkeypatch.setattr(dispatch.runtime, "call", fake_call)

    res = run(dispatch.run_agent("gmail", "abc19f"))
    draft = sp.get_draft(res["draft_id"])
    assert draft["kind"] == "email" and draft["target"] == "abc19f"
