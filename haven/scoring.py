"""LLM scoring for emails — produces tag/urgency/reply_needed/suggested_reply.

This is the Phase 1.4 layer on top of deterministic enrichment (Phase 1.3).
"""
from __future__ import annotations

import logging
from typing import Any

from haven import llm
from haven.sources.gmail import GmailItem

log = logging.getLogger(__name__)

DEFAULT_SCORE: dict[str, Any] = {
    "tag": "fyi",
    "urgency": "low",
    "reply_needed": False,
    "reply_reason": "",
    "summary": "",
    "suggested_action": "",
    "suggested_reply": "",
    "action_required": False,
}

VALID_TAGS = {"approval", "action", "fyi", "noise"}
VALID_URGENCY = {"low", "med", "high", "urgent"}


def build_email_prompt(item: GmailItem) -> str:
    body = (item.body_text or "")[:3000]
    if len(item.body_text or "") > 3000:
        body += "\n\n[... truncated]"

    thread_summary = ""
    if item.thread_message_count and item.thread_message_count > 1:
        owner = "you (Garth)" if item.garth_owns_last_turn else "someone else"
        thread_summary = f"Thread: {item.thread_message_count} messages, last from {owner}.\n"

    role = (item.garth_recipient_role or "").upper() or "?"
    company = item.sender_company or item.sender_domain or ""

    return f"""You are scoring an inbound email for Garth Thompson, CIO at Ayar Labs.

EMAIL
From: {item.sender_name} <{item.sender_email}> ({company})
Subject: {item.subject}
Date: {item.date}
Garth in: {role}
{thread_summary}
BODY:
{body}

Output ONLY a JSON object (no other text, no markdown fences):
{{
  "tag": "approval" | "action" | "fyi" | "noise",
  "urgency": "low" | "med" | "high" | "urgent",
  "action_required": true | false,
  "reply_needed": true | false,
  "reply_reason": "<why a reply is needed, or empty string>",
  "summary": "<one-sentence summary, <=120 chars>",
  "suggested_action": "<imperative phrase for Garth, <=60 chars>",
  "suggested_reply": "<one-line reply Garth could send as-is, or empty string>"
}}

CRITICAL FIELD — action_required:
  TRUE only when Garth must personally do something concrete: reply with an answer,
  approve a request, sign a document, complete a task, attend a specific event,
  decide between options, or unblock someone waiting on him.
  FALSE for fyi/awareness items even when the content is interesting or relevant.
  FALSE for items where someone else owns the action (a peer's update, a status
  digest, a notification that something already happened).
  This is the field used to pin items to the top of the dashboard, so be precise.

Garth's strict policy on what reaches him:

KEEP (tag = approval, action, or fyi):
- Anything from a real human (not an automated address) at Ayar Labs (ayarlabs.com)
- Anything from a known external contact Garth has working history with
- Anything explicitly asking Garth for approval, decision, sign-off, or action
- Any AR/ticket/issue assigned TO Garth specifically (Coupa, Asana, JIRA, NetSuite, Linear, etc.) — assignment, not status update
- Real-estate items (lease, permit, NDA, build-out, furniture, broker negotiations) -> action

REJECT as tag = noise (reply_needed=false, urgency=low):
- Newsletters, digests, mailing lists, marketing/promotional emails
- Sales prospecting / cold outreach / unsolicited demo asks
- Vendor "thought you'd be interested" pitches
- Automated status updates (Coupa "purchase received", Asana "task updated", JIRA "issue updated", "build succeeded", etc.) where Garth is NOT being asked to act
- Calendar accept/decline/cancel/tentative responses
- OOO / PTO / "automatic reply" notifications from anyone
- Event/dinner/webinar invitations from anyone NOT at ayarlabs.com
- Receipts, invoices auto-CC'd, account statements, login alerts, expiration warnings without action
- Generic platform notifications (LinkedIn/Twitter/Slack invite reminders, GitHub issue digests)
- Anything from a no-reply / notifications / alerts / billing address with no human ask
- Anything from outside Ayar that has no Ayar people on cc, no working history, no project relevance — even if it looks legitimate

GOOGLE WORKSPACE / DOCUSIGN / SERVICE NOTIFICATIONS — body content matters:
- "<Person> shared a document with you" / "You've been added to a shared drive" → if the sharer is at ayarlabs.com or a known approved external contact, tag = action (Garth needs to use the doc). Otherwise depends on context.
- DocuSign "<Person> sent you a document to sign" → if from Ayar or known external partner, tag = approval, urgency = high (signature blocks workflow).
- Google Calendar event subjects ("Invitation:" prefix) are caught by the structural filter — you won't see them.
- Workspace admin notices about Garth's own account → fyi or action depending on what's needed.
- Group ownership transfers, drive permission changes initiated by Ayar people → action.

SPECIFIC CASES requiring body-content judgment (these CANNOT be filtered by sender/subject alone — read the body):
- Bamboo / BambooHR / HRIS notifications → noise UNLESS the alert is specifically about Garth himself or someone on his direct team. "John Doe's birthday" or "Jane requested time off" where the person is NOT Garth's report → noise. A direct ask to Garth (review/approve a request) → action/approval.
- Password reset / account recovery → noise UNLESS the email body explicitly identifies Garth as the account holder ("password for garth@ayarlabs.com" or "your account, Garth"). Password resets for someone else's account, even if forwarded to Garth, → noise.
- Vendor "weekly roundup" / "service status" / "incident resolved" → noise UNLESS the body indicates Garth-specific impact, action required, or escalation.
- Outside event/dinner invites → noise unless from ayarlabs.com sender OR explicitly references a project Garth is currently driving (real estate, board prep, audit, vendor contract).

KEY DISTINCTION between fyi and noise:
- "fyi" = a real human at Ayar (or trusted external) sharing info Garth should be aware of but doesn't need to act on. Customer success update from a peer, internal heads-up, decision summary.
- "noise" = automated, marketing, or context-less external email. Default to noise when in doubt for outside-Ayar messages without clear context.

reply_needed=true ONLY when:
- Direct question to Garth
- Ask for approval/decision
- Deadline mentioned and Garth has not yet replied in-thread
- Vendor/customer/exec explicitly blocked on Garth
- Action item assigned specifically to Garth

urgency:
- urgent = needs reply <24h (named deadline, exec/customer impact, board prep)
- high   = needs reply <72h
- med    = this week
- low    = whenever / no real deadline

Tightness:
- summary <=120 chars; lead with the actual ask, not pleasantries
- suggested_action <=60 chars; imperative phrase
- suggested_reply <=200 chars; one line Garth could send as-is
"""


def _coerce(value: Any, valid: set[str], fallback: str) -> str:
    s = str(value).strip().lower()
    return s if s in valid else fallback


async def score_email(item: GmailItem) -> dict[str, Any]:
    prompt = build_email_prompt(item)
    try:
        # Haiku is plenty for email triage and ~3x faster than Sonnet — used by default.
        # Bump to LLM_MODEL (Sonnet) only if Haiku quality on a specific email is poor.
        result = await llm.claude_json(prompt, model=llm.config.LLM_MODEL_CHEAP)
    except Exception as e:
        log.warning("Score failed for %s: %s", item.msg_id, e)
        return {**DEFAULT_SCORE, "score_error": str(e)[:300]}

    try:
        return {
            "tag": _coerce(result.get("tag"), VALID_TAGS, "fyi"),
            "urgency": _coerce(result.get("urgency"), VALID_URGENCY, "low"),
            "action_required": bool(result.get("action_required", False)),
            "reply_needed": bool(result.get("reply_needed", False)),
            "reply_reason": str(result.get("reply_reason", ""))[:400],
            "summary": str(result.get("summary", ""))[:200],
            "suggested_action": str(result.get("suggested_action", ""))[:120],
            "suggested_reply": str(result.get("suggested_reply", ""))[:400],
        }
    except Exception as e:
        log.warning("Score parse failed for %s: %s (raw=%r)", item.msg_id, e, result)
        return {**DEFAULT_SCORE, "score_error": f"parse: {e}"}


async def score_emails_concurrent(
    items: list[GmailItem],
    max_concurrent: int = 3,
) -> list[dict[str, Any]]:
    """Score many items in parallel. Order matches the input list."""
    import asyncio

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(it: GmailItem) -> dict[str, Any]:
        async with sem:
            return await score_email(it)

    return await asyncio.gather(*[_one(i) for i in items])
