"""Otter.ai agent routes: status, items, cache, poll."""
from __future__ import annotations

import logging
import time
import traceback

from fastapi import APIRouter, HTTPException

from haven import config
from haven.db import cursor_store
from haven.events import bus
from haven.sources.otter import OtterFetcher

log = logging.getLogger("haven")

router = APIRouter(tags=["otter"])


@router.get("/api/auth/otter/status")
async def otter_status() -> dict:
    if not config.OTTER_API_KEY:
        return {"authed": False, "reason": "OTTER_API_KEY missing in .env"}
    from haven.sources.otter import OtterClient
    client = OtterClient()
    try:
        ws = await client.workspace()
        return {
            "authed": True,
            "workspace_id": ws.get("id"),
            "workspace_name": ws.get("name"),
            "owner_email": ((ws.get("owner") or {}).get("email") or "").lower(),
        }
    except Exception as e:
        return {"authed": False, "reason": str(e)}
    finally:
        await client.aclose()


@router.get("/api/agents/otter/items")
async def otter_items() -> dict:
    """Return cached Otter ARs (most recent meeting first).
    Filtered out: snoozed, rejected."""
    now = time.time()
    items = [
        i for i in cursor_store.list_cached("otter")
        if i.get("filter_status") != "reject"
        and float(i.get("snooze_until") or 0) <= now
    ]
    return {"items": items}


@router.post("/api/agents/otter/clear-cache")
async def otter_clear_cache() -> dict:
    n = cursor_store.clear_cached("otter")
    await bus.publish("otter_cache_cleared", {"count": n})
    return {"cleared": n}


@router.delete("/api/agents/otter/items/{msg_id:path}")
async def otter_dismiss_item(msg_id: str) -> dict:
    """Local-only dismiss — Otter has no AR-completed state in the API, so this
    just removes the item from Haven's cache."""
    removed = cursor_store.delete_cached("otter", msg_id)
    return {"removed": removed, "msg_id": msg_id}


@router.post("/api/agents/otter/poll")
async def otter_poll(force: bool = False) -> dict:
    """Pull recent Otter meetings → fetch each → emit ARs assigned to Garth.
    force=True wipes cache + rejections first so every AR is re-emitted."""
    if not config.OTTER_API_KEY:
        raise HTTPException(400, "Otter not authorized — OTTER_API_KEY missing in .env")

    fetcher = OtterFetcher()
    try:
        otter_ars = await fetcher.fetch_all()
    except Exception as e:
        log.error("Otter fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Otter fetch failed: {type(e).__name__}: {e}")

    if force:
        cursor_store.clear_cached("otter")
        cursor_store.clear_rejections("otter")
        cached_payloads: dict[str, dict] = {}
    else:
        cached_payloads = {p["msg_id"]: p for p in cursor_store.list_cached("otter")}

    items: list[dict] = []
    new_count = 0
    for it in otter_ars:
        payload = it.summary()
        prev = cached_payloads.get(it.msg_id)
        if prev:
            for k in (
                "linear_id", "linear_url", "linear_identifier", "linear_created_at",
                "handled_at", "snooze_until",
            ):
                if k in prev:
                    payload[k] = prev[k]
        else:
            new_count += 1
            await bus.publish("otter_item", payload)
        cursor_store.put_cached("otter", it.msg_id, payload)
        cursor_store.mark_seen("otter", it.msg_id)
        items.append(payload)

    summary = {
        "queried_total": len(otter_ars),
        "new_count": new_count,
        "from_cache": len(otter_ars) - new_count,
        "items": items,
    }
    await bus.publish(
        "otter_poll_complete",
        {k: v for k, v in summary.items() if k != "items"},
    )
    return summary
