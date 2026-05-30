"""Freshservice agent routes: status, items, cache, poll."""
from __future__ import annotations

import logging
import time
import traceback

from fastapi import APIRouter, HTTPException

from haven import config
from haven.db import cursor_store
from haven.events import bus
from haven.sources.freshservice import FreshserviceFetcher

log = logging.getLogger("haven")

router = APIRouter(tags=["freshservice"])


@router.get("/api/auth/freshservice/status")
async def freshservice_status() -> dict:
    if not config.FRESHSERVICE_API_KEY or not config.FRESHSERVICE_DOMAIN:
        return {"authed": False, "reason": "FRESHSERVICE_API_KEY or FRESHSERVICE_DOMAIN missing in .env"}
    from haven.sources.freshservice import FreshserviceClient, load_config as fs_load_config
    email = (fs_load_config().get("identity") or {}).get("email") or ""
    client = FreshserviceClient()
    try:
        agent_id = await client.self_agent_id(email or None)
        return {"authed": True, "agent_id": agent_id, "domain": config.FRESHSERVICE_DOMAIN}
    except Exception as e:
        return {"authed": False, "reason": str(e)}
    finally:
        await client.aclose()


@router.get("/api/agents/freshservice/items")
async def freshservice_items() -> dict:
    """Open tickets persist until next poll declares them closed — no freshness cutoff.
    Filtered out: snoozed, rejected."""
    now = time.time()
    items = [
        i for i in cursor_store.list_cached("freshservice")
        if i.get("filter_status") != "reject"
        and float(i.get("snooze_until") or 0) <= now
    ]
    return {"items": items}


@router.post("/api/agents/freshservice/clear-cache")
async def freshservice_clear_cache() -> dict:
    n = cursor_store.clear_cached("freshservice")
    await bus.publish("freshservice_cache_cleared", {"count": n})
    return {"cleared": n}


@router.post("/api/agents/freshservice/poll")
async def freshservice_poll(force: bool = False) -> dict:
    """Re-sync open tickets. Tickets removed from the result set (resolved/closed)
    are evicted from cache. force=True wipes cache first."""
    if not config.FRESHSERVICE_API_KEY or not config.FRESHSERVICE_DOMAIN:
        raise HTTPException(400, "Freshservice not authorized — FRESHSERVICE_API_KEY/FRESHSERVICE_DOMAIN missing")

    fetcher = FreshserviceFetcher()
    try:
        tickets = await fetcher.fetch_all()
    except Exception as e:
        log.error("Freshservice fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Freshservice fetch failed: {type(e).__name__}: {e}")

    fetched_ids = {t.msg_id for t in tickets}
    cached_payloads = {p["msg_id"]: p for p in cursor_store.list_cached("freshservice")}
    stale_ids = set(cached_payloads.keys()) - fetched_ids
    for sid in stale_ids:
        cursor_store.delete_cached("freshservice", sid)

    if force:
        cursor_store.clear_cached("freshservice")
        cached_payloads = {}

    items: list[dict] = []
    new_count = 0
    for t in tickets:
        payload = t.summary()
        prev = cached_payloads.get(t.msg_id)
        if prev:
            for k in (
                "linear_id", "linear_url", "linear_identifier", "linear_created_at",
                "handled_at", "snooze_until",
            ):
                if k in prev:
                    payload[k] = prev[k]
        else:
            new_count += 1
            await bus.publish("freshservice_item", payload)
        cursor_store.put_cached("freshservice", t.msg_id, payload)
        cursor_store.mark_seen("freshservice", t.msg_id)
        items.append(payload)

    summary = {
        "queried_total": len(tickets),
        "new_count": new_count,
        "from_cache": len(tickets) - new_count,
        "removed_stale": len(stale_ids),
        "items": items,
    }
    await bus.publish(
        "freshservice_poll_complete",
        {k: v for k, v in summary.items() if k != "items"},
    )
    return summary
