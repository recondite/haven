"""M1: promoted-case store, combined eval + regression delta, style distiller."""
import asyncio

import pytest

from haven import config
from haven import eval as eval_mod
from haven import spine as spine_mod
from haven.spine import Spine


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(eval_mod, "_PROMOTED_PATH", tmp_path / "data" / "state" / "eval-cases.json")
    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    return s


class Perfect(eval_mod.runtime.Runtime):
    """Always returns each case's expected label — accuracy should be 1.0."""
    name = "perfect"
    def __init__(self, cases):
        self._by_subject = {c["item"].subject: (c["tag"], c["urgency"]) for c in cases}
    async def call(self, prompt, model=None, timeout=60.0):
        return "{}"
    async def call_json(self, prompt, model=None, timeout=60.0):
        for subj, (tag, urg) in self._by_subject.items():
            if subj and subj in prompt:
                return {"tag": tag, "urgency": urg}
        return {"tag": "fyi", "urgency": "low"}


# ─── promotion ───────────────────────────────────────────
def test_promote_and_load(env):
    fields = {"sender_name": "Ada", "subject": "Approve PO-1", "body_text": "pending your approval"}
    n = eval_mod.promote_case(fields, "approval", "urgent", "promoted-po1")
    assert n == 1
    loaded = eval_mod.load_promoted()
    assert loaded[0]["tag"] == "approval" and loaded[0]["name"] == "promoted-po1"


def test_promote_dedups_by_name(env):
    f = {"subject": "x"}
    eval_mod.promote_case(f, "fyi", "low", "dupe")
    eval_mod.promote_case(f, "action", "high", "dupe")
    assert len(eval_mod.load_promoted()) == 1


def test_promote_coerces_invalid_labels(env):
    eval_mod.promote_case({"subject": "x"}, "banana", "whenever", "c")
    c = eval_mod.load_promoted()[0]
    assert c["tag"] == "fyi" and c["urgency"] == "low"


# ─── combined eval + delta ───────────────────────────────
def test_eval_includes_promoted_and_reports_delta(env):
    eval_mod.promote_case(
        {"sender_name": "Bob", "subject": "PROMOTED-CASE-SUBJ", "body_text": "pay this"},
        "approval", "urgent", "promoted-x")
    all_cases = eval_mod._all_cases()
    rt = Perfect(all_cases)
    r1 = run(eval_mod.run_eval(rt))
    assert r1["promoted_n"] == 1
    assert r1["n"] == len(eval_mod.GOLDEN) + 1
    assert r1["tag_accuracy"] == 1.0
    assert r1["delta_vs_last"] is None            # first run, no baseline
    r2 = run(eval_mod.run_eval(rt))
    assert r2["delta_vs_last"] == 0.0             # stable run -> zero delta


def test_eval_regression_shows_negative_delta(env):
    all_cases = eval_mod._all_cases()
    run(eval_mod.run_eval(Perfect(all_cases)))    # baseline 1.0

    class Broken(eval_mod.runtime.Runtime):
        name = "broken"
        async def call(self, prompt, model=None, timeout=60.0):
            return "{}"
        async def call_json(self, prompt, model=None, timeout=60.0):
            return {"tag": "noise", "urgency": "low"}   # wrong for most
    r = run(eval_mod.run_eval(Broken()))
    assert r["delta_vs_last"] < 0                  # regression is measured


# ─── style distiller (dormant until enough edits) ────────
def test_distill_dormant_without_edits(env):
    res = run(eval_mod.distill_style())
    assert res["ready"] is False and res["need"] == eval_mod._STYLE_MIN_EDITS


def test_distill_fires_with_enough_edits(env, tmp_path, monkeypatch):
    sb = tmp_path / "SecondBrain"
    (sb / "wiki" / "analyses").mkdir(parents=True)
    monkeypatch.setattr(config, "SECONDBRAIN_DIR", sb)
    from haven import knowledge
    monkeypatch.setattr(knowledge, "_WIKI_DIR", sb / "wiki")
    # seed 5 edited drafts
    for i in range(5):
        job = env.create_job("draft_reply_email", "cli", f"gmail/m{i}")
        did = env.create_draft(job, "email", f"m{i}", "edited final text " + str(i))
        env.edit_draft(did, "edited final text " + str(i))   # sets original_payload
        env.record_feedback(did, "edited", 12)

    async def fake_call(prompt, model=None, timeout=60.0):
        assert "GARTH APPROVED" in prompt
        return "- Lead with the answer\n- Sign off 'Thanks, GT'"
    monkeypatch.setattr(eval_mod.runtime, "call", fake_call)
    res = run(eval_mod.distill_style())
    assert res["ready"] is True and res.get("draft_id")
    d = env.get_draft(res["draft_id"])
    assert d["kind"] == "wiki" and "drafting style" in d["payload"].lower()
