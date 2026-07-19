"""LLM scoring for emails — produces tag/urgency/reply_needed/suggested_reply.

This is the Phase 1.4 layer on top of deterministic enrichment (Phase 1.3).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from haven import config, runtime
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

VALID_TAGS = {"approval", "action", "fyi", "noise", "travel"}
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
  "tag": "approval" | "action" | "fyi" | "noise" | "travel",
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

TRAVEL (tag = travel):
- Airline reservations / e-tickets / boarding passes / flight change or delay notices,
  hotel booking confirmations, car-rental confirmations, full trip itineraries.
- Use tag = travel even when the sender is a noreply/notifications address — these
  are NOT noise; Garth wants them surfaced and labeled.
- urgency: low by default; med if travel is within ~48h or the email is a
  schedule change / cancellation / check-in reminder for an imminent trip.
- reply_needed=false and action_required=false unless the email explicitly asks
  Garth to do something (e.g. confirm a change, complete check-in by a deadline).
- A human asking Garth to *book* or *approve* travel is action/approval, not travel.

GOOGLE WORKSPACE / DOCUSIGN / SERVICE NOTIFICATIONS — body content matters:
- "<Person> shared a document with you" / "You've been added to a shared drive" → if the sharer is at ayarlabs.com or a known approved external contact, tag = action (Garth needs to use the doc). Otherwise depends on context.
- DocuSign "<Person> sent you a document to sign" → if from Ayar or known external partner, tag = approval, urgency = high (signature blocks workflow).
- Coupa / procurement approval requests ("Action Required", "requires your approval", requisition/PO/expense pending approval) → tag = approval, urgency = urgent (blocks spend; Garth's sign-off is on the critical path).
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
        result = await runtime.call_json(prompt, model=config.LLM_MODEL_CHEAP)
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
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(it: GmailItem) -> dict[str, Any]:
        async with sem:
            return await score_email(it)

    return await asyncio.gather(*[_one(i) for i in items])


# ─── Slack scoring (Phase 2.0) ──────────────────────────────
def build_slack_prompt(item: dict) -> str:
    """`item` here is a SlackItem.summary() payload."""
    text = (item.get("body_text") or item.get("snippet") or "")[:2000]
    if len((item.get("body_text") or "")) > 2000:
        text += "\n\n[... truncated]"

    sender = item.get("sender_name") or item.get("sender_email") or item.get("sender_id") or "?"
    sender_email = item.get("sender_email") or ""
    chan_type = item.get("channel_type", "")
    chan_name = item.get("channel_name", "?")

    if chan_type == "im":
        venue = "1:1 DM directly to Garth"
    elif chan_type == "mpim":
        venue = f"group DM (#{chan_name}) including Garth"
    else:
        venue = f"channel #{chan_name}"

    flags: list[str] = []
    if item.get("is_dm"): flags.append("inbound DM")
    if item.get("is_mention"): flags.append("@-mentioned Garth")
    if item.get("is_watched_channel"): flags.append(f"posted in watched channel #{chan_name}")
    if item.get("is_watched_user"): flags.append(f"sender is on watch list (executive)")

    return f"""You are scoring an inbound Slack message for Garth Thompson, CIO at Ayar Labs.

CONTEXT
Venue: {venue}
From:  {sender} <{sender_email}>
Signal flags: {', '.join(flags) if flags else 'none'}
Channel: #{chan_name} ({chan_type})

MESSAGE
{text}

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
  TRUE when Garth must do something concrete: reply with an answer, approve a
  request, decide between options, attend a specific event, sign or review
  something, or unblock someone waiting on him.
  FALSE for fyi/awareness items even when interesting (status updates,
  team-wide announcements that don't require him personally to act).

CRITICAL FIELD — reply_needed:
  TRUE when:
    - Direct question to Garth
    - Ask for approval/decision/sign-off addressed to him
    - Someone explicitly blocked on Garth
    - Action item assigned specifically to Garth
  FALSE for: broadcast announcements, reactions/emojis as the only content,
  thread continuations where Garth isn't the addressee.

URGENCY GUIDANCE:
  - urgent = needs reply <24h (named deadline, exec/customer impact, prod issue)
  - high   = needs reply <72h
  - med    = this week
  - low    = whenever / no real deadline
  - DMs default to >= med (1:1 DMs imply intent — not low unless clearly fyi)
  - Messages from the watched-user (CEO Mark Wade) default to >= med
  - #elt-2026 messages: >= med (ELT context — never bury)

NOISE TAGGING (be conservative — don't over-noise things in #elt-2026 or DMs):
  - tag=noise for: pure automated bot summaries, GIF-only/emoji-only replies,
    "got it" / "thanks" acknowledgments with no further content,
    weekly bot digests posted to channels.
  - Substantive ELT updates: tag=fyi (NOT noise) — Garth needs to read them.

SLACK-SPECIFIC NOTES:
  - DMs from Ayar people are presumptively important (med+).
  - Long monologues in channels where Garth wasn't mentioned and not from a
    watched user → fyi or noise.
  - "@here" / "@channel" broadcast messages → fyi unless they ask Garth specifically.

Tightness:
- summary <=120 chars; lead with the ask
- suggested_action <=60 chars; imperative phrase
- suggested_reply <=200 chars; one line Garth could send as-is
"""


async def score_slack(item: dict) -> dict[str, Any]:
    prompt = build_slack_prompt(item)
    msg_id = item.get("msg_id", "?")
    try:
        result = await runtime.call_json(prompt, model=config.LLM_MODEL_CHEAP)
    except Exception as e:
        log.warning("Slack score failed for %s: %s", msg_id, e)
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
        log.warning("Slack score parse failed for %s: %s (raw=%r)", msg_id, e, result)
        return {**DEFAULT_SCORE, "score_error": f"parse: {e}"}
