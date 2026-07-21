"""Approval-tightening (SIM-182 pt2): tag="approval" is reserved for items Garth
must personally act on. Approval FYIs get demoted to fyi."""
import asyncio
from types import SimpleNamespace

from haven import runtime, scoring


def _gmail_item(**kw):
    base = dict(
        msg_id="m1", sender_name="Coupa", sender_email="no-reply@coupa.com",
        sender_company="", sender_domain="coupa.com", subject="s", body_text="b",
        date="2026-07-21", thread_message_count=1, garth_owns_last_turn=False,
        garth_recipient_role="to",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_tighten_approval_demotes_fyi_approval():
    # Approval tag but nothing for Garth to do → demoted.
    assert scoring._tighten_approval({"tag": "approval", "action_required": False})["tag"] == "fyi"
    # Approval tag AND he must act → kept.
    assert scoring._tighten_approval({"tag": "approval", "action_required": True})["tag"] == "approval"
    # Other tags untouched.
    assert scoring._tighten_approval({"tag": "action", "action_required": False})["tag"] == "action"


def test_score_email_wires_demotion(monkeypatch):
    async def fake_call_json(prompt, model=None, timeout=60.0):
        return {"tag": "approval", "action_required": False, "urgency": "low"}

    monkeypatch.setattr(runtime, "call_json", fake_call_json)
    res = asyncio.run(scoring.score_email(_gmail_item()))
    assert res["tag"] == "fyi"  # LLM said approval, but no action for Garth
