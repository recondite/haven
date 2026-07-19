"""Golden-set eval — gate runtime/model swaps on measured scoring quality.

Plan v4 Phase 4: "any runtime/model change re-runs the set and reports a measured
quality delta before promotion." First use here: the local-model (qwen via LM
Studio) scoring swap is ALREADY live, so this is the overdue safety net.

The set exercises the REAL email-scoring prompt (scoring.build_email_prompt)
through whatever runtime is active, so it measures prompt+model together. Cases
are deliberately unambiguous — a competent model gets them; a broken/mismatched
one won't. Seed set: expand with real approved drafts as feedback accumulates.

ponytail: SimpleNamespace stand-ins for GmailItem (only the ~10 attrs the prompt
reads), not full fixtures. Tag is the primary signal; urgency is fuzzier and
reported separately, not gated hard.
"""
from __future__ import annotations

from types import SimpleNamespace

from haven import runtime, scoring


def _item(**kw) -> SimpleNamespace:
    base = dict(
        sender_name="", sender_email="", sender_company="", sender_domain="",
        subject="", body_text="", date="2026-07-18", thread_message_count=1,
        garth_owns_last_turn=False, garth_recipient_role="to",
    )
    base.update(kw)
    return SimpleNamespace(**base)


GOLDEN: list[dict] = [
    {"name": "coupa_approval", "tag": "approval", "urgency": "urgent",
     "item": _item(sender_name="Coupa", sender_email="no-reply@coupa.com",
                   subject="Action Required: PO-8841 requires your approval",
                   body_text="Purchase requisition PO-8841 for $148,500 is pending your approval in Coupa. Approve or reject.")},
    {"name": "docusign_sign", "tag": "approval", "urgency": "high",
     "item": _item(sender_name="DocuSign", sender_email="dse@docusign.net",
                   subject="Nexterra sent you a document to sign",
                   body_text="Nexterra Foundry Services MSA is ready for your signature via DocuSign.")},
    {"name": "newsletter_noise", "tag": "noise", "urgency": "low",
     "item": _item(sender_name="TechCrunch", sender_email="newsletter@techcrunch.com",
                   subject="This week in AI: the biggest stories",
                   body_text="Your weekly roundup of AI news and top venture deals. Unsubscribe anytime.")},
    {"name": "cold_sales_noise", "tag": "noise", "urgency": "low",
     "item": _item(sender_name="Brandon at DataCorp", sender_email="brandon@datacorp.io", sender_domain="datacorp.io",
                   subject="Quick question about your data stack",
                   body_text="Hi Garth, I'd love 15 minutes to show you our platform. Are you free Thursday for a demo?")},
    {"name": "travel_itinerary", "tag": "travel", "urgency": "low",
     "item": _item(sender_name="Concur Travel", sender_email="no-reply@concur.com",
                   subject="Your trip to Taipei is confirmed",
                   body_text="Flight UA872 SFO to TPE confirmed. Hotel: Grand Hyatt Taipei, 3 nights. Full itinerary attached.")},
    {"name": "internal_deadline_ask", "tag": "action", "urgency": "high",
     "item": _item(sender_name="Dana Whitfield", sender_email="dana@ayarlabs.com",
                   sender_company="Ayar Labs", sender_domain="ayarlabs.com", garth_recipient_role="to",
                   subject="Need your capex slide for Thursday's board deck",
                   body_text="Garth — can you send me your capex slide by Thursday? It's blocking the board pre-read.")},
    {"name": "ooo_noise", "tag": "noise", "urgency": "low",
     "item": _item(sender_name="Marcus Feld", sender_email="marcus@ayarlabs.com", sender_domain="ayarlabs.com",
                   subject="Automatic reply: Out of office",
                   body_text="I am out of office until Monday with limited email access. For urgent matters contact IT.")},
    {"name": "internal_fyi", "tag": "fyi", "urgency": "low",
     "item": _item(sender_name="Priya Raghavan", sender_email="priya@ayarlabs.com",
                   sender_company="Ayar Labs", sender_domain="ayarlabs.com", garth_recipient_role="cc",
                   subject="Heads up: vendor onboarding went smoothly",
                   body_text="Just closing the loop — the CrowdStrike onboarding finished with no issues. No action needed, sharing for awareness.")},
]


async def run_eval(rt: runtime.Runtime | None = None) -> dict:
    """Score every golden case through the active (or given) runtime; report
    tag/urgency accuracy. On-demand (a full run hits the model N times)."""
    rt = rt or runtime.get_runtime()
    cases = []
    tag_ok = urg_ok = 0
    for g in GOLDEN:
        prompt = scoring.build_email_prompt(g["item"])
        try:
            res = await rt.call_json(prompt, model=scoring.config.LLM_MODEL_CHEAP, timeout=120)
            tag = scoring._coerce(res.get("tag"), scoring.VALID_TAGS, "fyi")
            urg = scoring._coerce(res.get("urgency"), scoring.VALID_URGENCY, "low")
            err = None
        except Exception as e:  # noqa: BLE001
            tag = urg = None
            err = str(e)[:200]
        t_ok = tag == g["tag"]
        u_ok = urg == g["urgency"]
        tag_ok += int(t_ok)
        urg_ok += int(u_ok)
        cases.append({"name": g["name"], "expected_tag": g["tag"], "got_tag": tag,
                      "tag_ok": t_ok, "expected_urgency": g["urgency"], "got_urgency": urg,
                      "urgency_ok": u_ok, "error": err})
    n = len(GOLDEN)
    return {
        "runtime": rt.name, "model": (scoring.config.LOCAL_LLM_MODEL
                                      if rt.name == "local" else scoring.config.LLM_MODEL_CHEAP),
        "n": n,
        "tag_accuracy": round(tag_ok / n, 3),
        "urgency_accuracy": round(urg_ok / n, 3),
        "cases": cases,
    }
