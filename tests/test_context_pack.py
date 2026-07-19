"""M2: freshness-aware search, sections, context packs, grounded dispatch."""
import asyncio

import pytest

from haven import config, dispatch, knowledge
from haven import spine as spine_mod
from haven.spine import Spine


def run(coro):
    return asyncio.run(coro)


PERSON = """---
type: person
tags: [ayar-labs]
created: 2026-04-28
updated: 2026-05-29
---

# Dana Whitfield

**Title:** CFO
**Work email:** dana@ayarlabs.com

## Summary

Chief Financial Officer; owns board deck and capex envelope.

## Programs

Runs quarterly capex reviews.
"""

TOPIC = """---
type: project
tags: [photonics]
created: 2026-01-01
updated: 2026-07-01
---

# Garfield

## Summary

Garfield is the next-gen test bench program.

## Schedule

Bring-up gated on the Keysight LCA bench delivery.
"""

DEPRECATED = """---
type: project
status: deprecated
created: 2025-01-01
updated: 2025-02-01
---

# Garfield Legacy Plan

## Summary

Old Garfield plan, superseded — Keysight bench details here are stale.
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Temp SecondBrain + temp spine with a roster person wired everywhere."""
    sb = tmp_path / "SecondBrain"
    (sb / "wiki" / "entities" / "people").mkdir(parents=True)
    (sb / "wiki" / "projects").mkdir(parents=True)
    (sb / "wiki" / "entities" / "people" / "dana-whitfield.md").write_text(PERSON, encoding="utf-8")
    (sb / "wiki" / "projects" / "garfield.md").write_text(TOPIC, encoding="utf-8")
    (sb / "wiki" / "projects" / "garfield-legacy.md").write_text(DEPRECATED, encoding="utf-8")
    monkeypatch.setattr(config, "SECONDBRAIN_DIR", sb)
    monkeypatch.setattr(knowledge, "_WIKI_DIR", sb / "wiki")

    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    monkeypatch.setattr(dispatch, "spine", s)
    s.upsert_person("dana-whitfield", "Dana Whitfield", "CFO", "Finance", "Mark Wade",
                    "dana@ayarlabs.com")
    return s


# ─── freshness-aware search ──────────────────────────────
def test_search_carries_freshness(world):
    hits = knowledge.search("Garfield test bench")
    top = hits[0]
    assert top["updated"] == "2026-07-01" and top["status"] is None
    assert isinstance(top["age_days"], int)


def test_search_excludes_deprecated_by_default(world):
    paths = [h["path"] for h in knowledge.search("Garfield Keysight stale legacy")]
    assert not any("legacy" in p for p in paths)
    with_dep = [h["path"] for h in knowledge.search("Garfield Keysight stale legacy", include_deprecated=True)]
    assert any("legacy" in p for p in with_dep)


# ─── sections ────────────────────────────────────────────
def test_best_section_picks_matching_heading(world):
    body = knowledge._strip_frontmatter(TOPIC)
    sec = knowledge._best_section(body, {"schedule", "keysight"})
    assert sec["heading"] == "Schedule"
    assert "Keysight" in sec["text"]


# ─── context packs ───────────────────────────────────────
def test_pack_resolves_sender_and_topic(world):
    item = {"sender": "Dana Whitfield <dana@ayarlabs.com>",
            "subject": "Garfield schedule question", "snippet": "when does the Keysight bench arrive?"}
    pack = knowledge.context_pack(item)
    whys = {c["why"] for c in pack["citations"]}
    assert "sender" in whys and "topic" in whys
    sender_c = next(c for c in pack["citations"] if c["why"] == "sender")
    assert sender_c["path"].endswith("dana-whitfield.md")
    topic_c = next(c for c in pack["citations"] if c["why"] == "topic")
    assert topic_c["path"].endswith("garfield.md")
    assert all(c["sha"] and len(c["sha"]) == 8 for c in pack["citations"])
    assert not any("legacy" in c["path"] for c in pack["citations"])  # deprecated excluded


def test_pack_unknown_sender_no_guess(world):
    pack = knowledge.context_pack({"sender": "stranger@outside.com", "subject": "hello there"})
    assert not any(c["why"] == "sender" for c in pack["citations"])


def test_pack_respects_budget(world):
    item = {"sender": "dana@ayarlabs.com", "subject": "Garfield Keysight schedule"}
    pack = knowledge.context_pack(item, budget_chars=300)
    assert sum(len(f["text"]) for f in pack["fragments"]) <= 340  # budget + ellipsis slack


def test_render_pack_numbers_fragments(world):
    pack = knowledge.context_pack({"sender": "dana@ayarlabs.com", "subject": "Garfield"})
    text = knowledge.render_pack(pack)
    assert "[1]" in text and "SECONDBRAIN" in text
    assert knowledge.render_pack({"fragments": [], "citations": []}) == ""


# ─── ask (M2A) ───────────────────────────────────────────
def test_ask_answers_from_wiki_only(world, monkeypatch):
    from haven import runtime as rt
    async def fake_call(prompt, model=None, timeout=60.0):
        assert "ONLY the SecondBrain wiki context" in prompt
        assert "Garfield" in prompt              # retrieved context present
        return "Bring-up is gated on the Keysight bench [1]."
    monkeypatch.setattr(rt, "call", fake_call)
    res = run(knowledge.ask("When does Garfield bring-up start?"))
    assert res["answered"] is True
    assert any(c["path"].endswith("garfield.md") for c in res["citations"])


def test_ask_honest_miss_no_pages(world, monkeypatch):
    res = run(knowledge.ask("zebra kayak thermodynamics"))
    assert res["answered"] is False and res["citations"] == []


def test_ask_honest_miss_model_says_not_in_wiki(world, monkeypatch):
    from haven import runtime as rt
    async def fake_call(prompt, model=None, timeout=60.0):
        return "NOT_IN_WIKI"
    monkeypatch.setattr(rt, "call", fake_call)
    res = run(knowledge.ask("Garfield budget line owner?"))
    assert res["answered"] is False
    assert res["citations"]                      # shows what was checked


def test_ask_includes_summary_section(world, monkeypatch):
    """Question-style queries get the lead facts even when a detail section
    scores higher (the Mark-Wade-reports-to regression)."""
    from haven import runtime as rt
    captured = {}
    async def fake_call(prompt, model=None, timeout=60.0):
        captured["prompt"] = prompt
        return "ok [1]"
    monkeypatch.setattr(rt, "call", fake_call)
    run(knowledge.ask("Garfield schedule Keysight"))
    assert "#Summary" in captured["prompt"] or "Summary:" in captured["prompt"]


# ─── grounded dispatch ───────────────────────────────────
def test_draft_evidence_carries_wiki_citations(world, monkeypatch):
    class FakeStore:
        def get_cached_payloads(self, source, ids):
            return {ids[0]: {"msg_id": "C1:9.9", "sender": "Dana Whitfield <dana@ayarlabs.com>",
                             "snippet": "Garfield Keysight bench timing?"}}
    monkeypatch.setattr(dispatch, "cursor_store", FakeStore())

    async def fake_call(prompt, model=None, timeout=60.0):
        assert "SECONDBRAIN" in prompt          # the pack actually reached the model
        return "Bench lands next month. [1]"
    monkeypatch.setattr(dispatch.runtime, "call", fake_call)

    res = run(dispatch.run_agent("slack", "C1:9.9"))
    d = world.get_draft(res["draft_id"])
    import json as _json
    ev = _json.loads(d["evidence"])
    wiki = [e for e in ev if e.get("source") == "secondbrain"]
    assert wiki and all("sha" in e and "path" in e for e in wiki)
