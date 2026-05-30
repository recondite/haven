"""Wiki ingest + page listing routes."""
from __future__ import annotations

import logging
import time
import traceback

from fastapi import APIRouter, HTTPException

from haven import wiki
from haven.db import cursor_store
from haven.deps import gmail_auth
from haven.events import bus
from haven.sources.gmail import GmailFetcher

log = logging.getLogger("haven")

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


@router.post("/ingest")
async def wiki_ingest(payload: dict) -> dict:
    """Ingest a single source into the curated wiki.

    payload = {"source": "gmail", "msg_id": "..."}
    """
    source = payload.get("source", "gmail")
    msg_id = payload.get("msg_id")
    if not msg_id:
        raise HTTPException(400, "msg_id required")

    if source != "gmail":
        raise HTTPException(400, f"Unsupported source: {source}")

    cached = cursor_store.get_cached_payloads("gmail", [msg_id])
    item = cached.get(msg_id)
    if not item:
        raise HTTPException(404, f"msg_id {msg_id} not in cache — poll first")

    if not gmail_auth.is_authed():
        raise HTTPException(400, "Gmail not authorized")

    # Re-fetch the body. The cache stores summaries (no body) to keep rows light.
    fetcher = GmailFetcher(auth=gmail_auth, queries=[])
    try:
        full_item = await fetcher.fetch_message(msg_id)
        body_text = full_item.body_text
    except Exception as e:
        log.error("Wiki ingest fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Body fetch failed: {e}")

    try:
        result = await wiki.ingest_source(item, body_text)
    except Exception as e:
        log.error("Wiki ingest failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Wiki ingest failed: {e}")

    item["wiki_ingested"] = True
    item["wiki_ingested_at"] = time.time()
    cursor_store.put_cached("gmail", msg_id, item)

    await bus.publish(
        "wiki_ingested",
        {"msg_id": msg_id, "files": result["files_written"], "log_entry": result["log_entry"]},
    )
    return result


@router.get("/pages")
async def wiki_pages() -> dict:
    """List all wiki pages — useful for a future browse UI."""
    return {"pages": [str(p.relative_to(wiki.WIKI_DIR).as_posix()) for p in wiki.list_pages()]}
