"""Executor — the ONLY module that turns an approved draft into an action.

Live send built with GT's explicit sign-off (2026-07-19, Slack + Gmail). Armed
by HAVEN_SEND_MODE=live in .env (default: dry — approve records the action,
nothing transmits).

At-most-once send protocol:
  1. INSERT action row with status='sending' — the UNIQUE(draft_id) constraint
     claims the slot, so a double-click / concurrent approve / restart can never
     produce a second send.
  2. Perform the send.
  3. UPDATE the row to 'sent' (with the provider's message id).
If the process dies between 2 and 3 the row stays 'sending' and Haven does NOT
retry — the approvals API surfaces it as needs-verify. Never twice > maybe once.

A transport failure marks the action 'failed' and does NOT auto-retry.
ponytail: no resend/reset flow yet — if a transient Slack blip marks a send
failed, re-run the draft agent; add an explicit reset endpoint if it recurs.

No-delete enforcement: the transport table below contains exactly two verbs —
post a Slack message, send a Gmail reply. No delete/admin API is imported or
called anywhere in this module (tested).
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import logging
import re
from email.message import EmailMessage

from haven import config
from haven.spine import spine

log = logging.getLogger("haven")

ALLOWED_KINDS = {"slack", "email", "task", "wiki"}


class ExecutorError(Exception):
    pass


def is_dry_run() -> bool:
    return config.SEND_MODE != "live"


# ─── Transports (the ONLY outbound verbs Haven has) ─────
async def _slack_post(target: str, payload: str) -> dict:
    """Post a reply into the originating Slack thread. target = 'channel:ts'."""
    from haven.sources.slack import SlackClient
    channel, _, ts = target.partition(":")
    if not channel or not ts:
        raise ExecutorError(f"bad slack target {target!r} (want channel:ts)")
    client = SlackClient()
    try:
        resp = await client._call(
            "chat.postMessage",
            {"channel": channel, "text": payload, "thread_ts": ts},
            use_bot=True,
        )
    finally:
        await client.aclose()
    return {"provider": "slack", "channel": channel, "ts": resp.get("ts"),
            "thread_ts": ts}


# SecondBrain ingest (Phase 3). A local, schema-validated, approval-gated write —
# NEW pages only (never overwrites/deletes: ground rule #1 for SecondBrain).
_WIKI_TYPES = {"person", "company", "team", "concept", "project", "source",
               "analysis", "overview", "tool"}
_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_H1_RE = re.compile(r"^#\s+\S", re.M)


def validate_wiki(payload: str, target: str) -> None:
    """Raise ExecutorError unless the draft is a schema-valid, new SecondBrain
    page. Called before an ingest draft can be approved."""
    m = _FM_RE.match(payload or "")
    if not m:
        raise ExecutorError("wiki draft missing YAML frontmatter (--- ... ---)")
    fm = m.group(1)
    tm = re.search(r"^type:\s*(\w+)", fm, re.M)
    if not tm or tm.group(1) not in _WIKI_TYPES:
        raise ExecutorError(f"wiki frontmatter needs type in {sorted(_WIKI_TYPES)}")
    for key in ("created", "updated"):
        if not re.search(rf"^{key}:\s*\d{{4}}-\d{{2}}-\d{{2}}", fm, re.M):
            raise ExecutorError(f"wiki frontmatter needs {key}: YYYY-MM-DD")
    if not _H1_RE.search(payload):
        raise ExecutorError("wiki draft needs an H1 (# Title)")
    rel = (target or "").replace("\\", "/")
    if not rel.startswith("wiki/") or not rel.endswith(".md"):
        raise ExecutorError("wiki target must be a wiki/...md path")
    dest = (config.SECONDBRAIN_DIR / rel).resolve()
    try:
        dest.relative_to((config.SECONDBRAIN_DIR / "wiki").resolve())
    except ValueError:
        raise ExecutorError("wiki target escapes the wiki/ tree")
    if dest.exists():
        raise ExecutorError(f"page already exists: {rel} — updates go through the drift flow, not ingest")


async def _wiki_write(target: str, payload: str) -> dict:
    """Write a new SecondBrain page + append to the append-only log. New pages
    only — validate_wiki (run at approve time) guarantees it doesn't exist."""
    rel = target.replace("\\", "/")
    dest = config.SECONDBRAIN_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(payload, encoding="utf-8")
    log_path = config.SECONDBRAIN_DIR / "wiki" / "log.md"
    if log_path.exists():
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n- Haven ingest: created [[{dest.stem}]] ({rel})\n")
    return {"provider": "secondbrain", "path": rel}


async def _gmail_send_reply(target: str, payload: str) -> dict:
    """Send a reply in the Gmail thread of message `target` (a Gmail msg_id).
    Addressing comes from Gmail's own headers for that message — never from
    draft content."""
    from haven.deps import gmail_auth
    service = await gmail_auth.get_service()

    def _get_meta() -> dict:
        return service.users().messages().get(
            userId="me", id=target, format="metadata",
            metadataHeaders=["Message-ID", "Subject", "From", "Reply-To"],
        ).execute()

    meta = await asyncio.to_thread(_get_meta)
    headers = {h["name"].lower(): h["value"]
               for h in meta.get("payload", {}).get("headers", [])}
    to = headers.get("reply-to") or headers.get("from")
    if not to:
        raise ExecutorError(f"gmail {target}: no From/Reply-To header to address")
    subject = headers.get("subject", "")
    if subject.lower()[:3] != "re:":
        subject = f"Re: {subject}"

    mime = EmailMessage()
    mime["To"] = to
    mime["Subject"] = subject
    orig_msgid = headers.get("message-id")
    if orig_msgid:
        mime["In-Reply-To"] = orig_msgid
        mime["References"] = orig_msgid
    mime.set_content(payload)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    def _send() -> dict:
        return service.users().messages().send(
            userId="me", body={"raw": raw, "threadId": meta.get("threadId")}
        ).execute()

    sent = await asyncio.to_thread(_send)
    return {"provider": "gmail", "id": sent.get("id"), "threadId": sent.get("threadId"), "to": to}


_TRANSPORTS = {"slack": _slack_post, "email": _gmail_send_reply, "wiki": _wiki_write}


async def approve(draft_id: int, actor: str = "gt") -> dict:
    """Approve a pending draft -> exactly one action, at most one send."""
    draft = spine.get_draft(draft_id)
    if draft is None:
        raise ExecutorError(f"draft {draft_id} not found")
    if draft["status"] == "rejected":
        raise ExecutorError(f"draft {draft_id} was rejected; cannot approve")
    if draft["kind"] not in ALLOWED_KINDS:
        raise ExecutorError(f"draft {draft_id} has disallowed kind {draft['kind']!r}")
    if draft["kind"] == "wiki":
        validate_wiki(draft["payload"], draft["target"])  # schema-invalid ingest can't be approved

    dry = is_dry_run()
    initial_status = "dry_run" if dry else "sending"
    initial_result = ({"dry_run": True, "note": "no external send performed"}
                      if dry else None)

    # Step 1: claim the draft's single action slot (idempotency barrier).
    action_id, created = spine.record_action(
        draft_id, draft["kind"], draft["target"], initial_status, initial_result
    )
    if not created:
        # Someone already approved this draft. Report the existing state; a row
        # stuck in 'sending' means a crash mid-send — needs manual verify.
        existing = spine.get_action_for_draft(draft_id)
        return {"draft_id": draft_id, "action_id": action_id, "created": False,
                "status": existing["status"], "dry_run": dry,
                "needs_verify": existing["status"] == "sending"}

    # First (and only) approval: bookkeeping.
    spine.set_draft_status(draft_id, "approved")
    orig = draft.get("original_payload")
    if orig is not None and orig != draft["payload"]:
        spine.record_feedback(draft_id, "edited", _edit_distance(orig, draft["payload"]))
    else:
        spine.record_feedback(draft_id, "approved_clean")
    spine.audit(actor, "draft_approved", "draft", draft_id, {"action_id": action_id})

    status = initial_status
    result = initial_result
    if not dry:
        # Step 2 + 3: send, then advance the row. Kinds without a live transport
        # (task/wiki) stay draft-recorded only.
        transport = _TRANSPORTS.get(draft["kind"])
        if transport is None:
            status, result = "failed", {"error": f"no live transport for kind {draft['kind']!r}"}
        else:
            try:
                result = await transport(draft["target"], draft["payload"])
                status = "sent"
            except Exception as e:  # noqa: BLE001 — recorded, surfaced, never retried
                log.error("live send failed for draft %s: %s", draft_id, e)
                status, result = "failed", {"error": str(e)[:500]}
        spine.update_action(action_id, status, result)

    spine.audit("system", "action_executed", "action", action_id,
                {"status": status, "dry_run": dry})
    return {"draft_id": draft_id, "action_id": action_id, "created": True,
            "status": status, "dry_run": dry, "result": result}


def _edit_distance(a: str, b: str) -> int:
    """Characters changed between original and edited draft (stdlib difflib —
    insert/delete/replace opcode spans, close enough for a feedback signal)."""
    sm = difflib.SequenceMatcher(None, a, b)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")


def edit(draft_id: int, new_payload: str, actor: str = "gt") -> dict:
    """Edit a PENDING draft's text. What's approved is what would be sent."""
    draft = spine.get_draft(draft_id)
    if draft is None:
        raise ExecutorError(f"draft {draft_id} not found")
    if draft["status"] != "pending":
        raise ExecutorError(f"draft {draft_id} is {draft['status']}; only pending drafts can be edited")
    new_payload = (new_payload or "").strip()
    if not new_payload:
        raise ExecutorError("edited draft cannot be empty")
    spine.edit_draft(draft_id, new_payload)
    return {"draft_id": draft_id, "payload": new_payload, "edited": True}


def reject(draft_id: int, reason: str = "", actor: str = "gt") -> dict:
    draft = spine.get_draft(draft_id)
    if draft is None:
        raise ExecutorError(f"draft {draft_id} not found")
    spine.set_draft_status(draft_id, "rejected")
    spine.record_feedback(draft_id, "rejected")
    spine.audit(actor, "draft_rejected", "draft", draft_id, {"reason": reason[:300]})
    return {"draft_id": draft_id, "status": "rejected"}
