"""Asana agent routes: status, items, cache, poll."""
from __future__ import annotations

import logging
import time
import traceback

from fastapi import APIRouter, HTTPException

from haven import config
from haven.db import cursor_store
from haven.events import bus
from haven.sources.asana import AsanaFetcher, parse_iso

log = logging.getLogger("haven")

router = APIRouter(tags=["asana"])

_PRESERVED_KEYS = (
    "linear_id", "linear_url", "linear_identifier", "linear_created_at",
    "handled_at", "snooze_until",
)


@router.get("/api/auth/asana/status")
async def asana_status() -> dict:
    if not config.ASANA_TOKEN:
        return {"authed": False, "reason": "ASANA_TOKEN missing in .env"}
    from haven.sources.asana import AsanaClient
    client = AsanaClient()
    try:
        me = await client.me()
        return {"authed": True, "account_gid": me["gid"], "workspace": me["workspace_gid"]}
    except Exception as e:
        return {"authed": False, "reason": str(e)}
    finally:
        await client.aclose()


@router.get("/api/agents/asana/items")
async def asana_items() -> dict:
    now = time.time()
    items = [
        i for i in cursor_store.list_cached("asana")
        if i.get("filter_status") != "reject"
        and float(i.get("snooze_until") or 0) <= now
    ]
    return {"items": items}


@router.post("/api/agents/asana/clear-cache")
async def asana_clear_cache() -> dict:
    n = cursor_store.clear_cached("asana")
    await bus.publish("asana_cache_cleared", {"count": n})
    return {"cleared": n}


def merge_prev(payload: dict, prev: dict | None) -> dict:
    """Carry local state forward across re-polls; resurface if the task changed
    in Asana after being marked done in Haven."""
    if not prev:
        return payload
    for k in _PRESERVED_KEYS:
        if k in prev:
            payload[k] = prev[k]
    if payload.get("handled_at"):
        modified = parse_iso(payload.get("date"))
        if modified and modified.timestamp() > float(payload["handled_at"]):
            payload.pop("handled_at", None)
    return payload


@router.post("/api/agents/asana/poll")
async def asana_poll(force: bool = False) -> dict:
    """Re-sync incomplete assigned tasks. Tasks that left the set (completed /
    reassigned) are evicted. force=True wipes cache first."""
    if not config.ASANA_TOKEN:
        raise HTTPException(400, "Asana not authorized — ASANA_TOKEN missing")

    fetcher = AsanaFetcher()
    try:
        tasks = await fetcher.fetch_all()
    except Exception as e:
        log.error("Asana fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Asana fetch failed: {type(e).__name__}: {e}")

    fetched_ids = {t.msg_id for t in tasks}
    cached_payloads = {p["msg_id"]: p for p in cursor_store.list_cached("asana")}
    stale_ids = set(cached_payloads.keys()) - fetched_ids
    for sid in stale_ids:
        cursor_store.delete_cached("asana", sid)

    if force:
        cursor_store.clear_cached("asana")
        cached_payloads = {}

    items: list[dict] = []
    new_count = 0
    for t in tasks:
        payload = t.summary()
        prev = cached_payloads.get(t.msg_id)
        if prev:
            merge_prev(payload, prev)
        else:
            new_count += 1
            await bus.publish("asana_item", payload)
        cursor_store.put_cached("asana", t.msg_id, payload)
        cursor_store.mark_seen("asana", t.msg_id)
        items.append(payload)

    summary = {
        "queried_total": len(tasks),
        "new_count": new_count,
        "from_cache": len(tasks) - new_count,
        "removed_stale": len(stale_ids),
        "items": items,
    }
    await bus.publish(
        "asana_poll_complete",
        {k: v for k, v in summary.items() if k != "items"},
    )
    return summary
