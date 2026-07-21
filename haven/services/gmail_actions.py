"""Gmail write actions shared across routers.

"Done" in Haven mirrors into Gmail so the two stay in sync: the message is
marked **read** (remove UNREAD), **archived** (remove INBOX), and tagged with a
**Haven/Done** label so the action is visible in Gmail. All non-destructive per
Haven ground rules — the message stays in All Mail, recoverable. Never
delete/trash.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from haven.deps import gmail_auth
from haven.events import bus

log = logging.getLogger("haven")

DONE_LABEL = "Haven/Done"
_done_label_id: str | None = None


def _ensure_done_label_sync(service) -> str:
    """Return the Gmail label id for DONE_LABEL, creating it once and caching it.
    Runs inside the to_thread body (blocking Google client calls)."""
    global _done_label_id
    if _done_label_id:
        return _done_label_id
    http = gmail_auth.new_http()
    existing = service.users().labels().list(userId="me").execute(http=http)
    for lb in existing.get("labels", []):
        if lb.get("name") == DONE_LABEL:
            _done_label_id = lb["id"]
            return _done_label_id
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={"name": DONE_LABEL, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        )
        .execute(http=http)
    )
    _done_label_id = created["id"]
    return _done_label_id


async def gmail_service():
    """Authorized Gmail service, or HTTP 400 if not connected. Refresh-safe +
    cached — delegates to the shared GmailAuth.get_service()."""
    service = await gmail_auth.get_service()
    if service is None:
        raise HTTPException(400, "Gmail not authorized")
    return service


async def archive_id(msg_id: str) -> None:
    """Mark one Gmail message done: read (−UNREAD) + archived (−INBOX) + Haven/Done
    label. Non-destructive. Raises HTTPException on failure. Shared by the archive
    route and the Gmail branch of item mark-done."""
    service = await gmail_service()

    def _do() -> dict:
        label_id = _ensure_done_label_sync(service)
        return (
            service.users()
            .messages()
            .modify(
                userId="me",
                id=msg_id,
                body={"removeLabelIds": ["INBOX", "UNREAD"], "addLabelIds": [label_id]},
            )
            .execute(http=gmail_auth.new_http())
        )

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        log.error("Archive %s failed: %s", msg_id, e)
        raise HTTPException(500, f"Archive failed: {e}")

    await bus.publish("gmail_item_archived", {"msg_id": msg_id})


async def archive_ids(msg_ids: list[str]) -> None:
    """Remove the INBOX label from many messages in one batchModify call.

    Non-destructive (messages stay in All Mail). Used by thread-level mark-done so
    an entire Gmail conversation is archived in a single API round-trip. Raises
    HTTPException on failure.
    """
    if not msg_ids:
        return
    service = await gmail_service()

    def _do() -> None:
        label_id = _ensure_done_label_sync(service)
        service.users().messages().batchModify(
            userId="me",
            body={"ids": msg_ids, "addLabelIds": [label_id],
                  "removeLabelIds": ["INBOX", "UNREAD"]},
        ).execute(http=gmail_auth.new_http())

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        log.error("Batch archive failed (%d ids): %s", len(msg_ids), e)
        raise HTTPException(500, f"Batch archive failed: {e}")

    for mid in msg_ids:
        await bus.publish("gmail_item_archived", {"msg_id": mid})
