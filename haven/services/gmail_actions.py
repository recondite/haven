"""Gmail write actions shared across routers (archive = INBOX label removal).

Per Haven ground rules these are non-destructive: archive removes the INBOX
label only (message stays in All Mail, recoverable). Never delete/trash.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from haven.deps import gmail_auth
from haven.events import bus

log = logging.getLogger("haven")


async def gmail_service():
    """Authorized Gmail service, or HTTP 400 if not connected. Refresh-safe +
    cached — delegates to the shared GmailAuth.get_service()."""
    service = await gmail_auth.get_service()
    if service is None:
        raise HTTPException(400, "Gmail not authorized")
    return service


async def archive_id(msg_id: str) -> None:
    """Remove the INBOX label from one Gmail message. Non-destructive. Raises
    HTTPException on failure. Shared by the archive route and the Gmail branch of
    item mark-done."""
    service = await gmail_service()

    def _do() -> dict:
        return (
            service.users()
            .messages()
            .modify(userId="me", id=msg_id, body={"removeLabelIds": ["INBOX"]})
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
        service.users().messages().batchModify(
            userId="me",
            body={"ids": msg_ids, "removeLabelIds": ["INBOX"]},
        ).execute(http=gmail_auth.new_http())

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        log.error("Batch archive failed (%d ids): %s", len(msg_ids), e)
        raise HTTPException(500, f"Batch archive failed: {e}")

    for mid in msg_ids:
        await bus.publish("gmail_item_archived", {"msg_id": mid})
