"""Jira agent routes: status, items, cache, poll."""
from __future__ import annotations

import logging
import time
import traceback

from fastapi import APIRouter, HTTPException

from haven import config
from haven.db import cursor_store
from haven.events import bus
from haven.sources.jira import JiraFetcher, parse_iso

log = logging.getLogger("haven")

router = APIRouter(tags=["jira"])

# Local state preserved across re-polls of the same issue.
_PRESERVED_KEYS = (
    "linear_id", "linear_url", "linear_identifier", "linear_created_at",
    "handled_at", "snooze_until",
)


def _jira_configured() -> bool:
    return bool(config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN)


@router.get("/api/auth/jira/status")
async def jira_status() -> dict:
    if not _jira_configured():
        return {"authed": False, "reason": "JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN missing in .env"}
    from haven.sources.jira import JiraClient
    client = JiraClient()
    try:
        account_id = await client.myself()
        return {"authed": True, "account_id": account_id, "base_url": client.base_url}
    except Exception as e:
        return {"authed": False, "reason": str(e)}
    finally:
        await client.aclose()


@router.get("/api/agents/jira/items")
async def jira_items() -> dict:
    """Open issues persist until next poll declares them gone — no freshness cutoff.
    Filtered out: snoozed, rejected."""
    now = time.time()
    items = [
        i for i in cursor_store.list_cached("jira")
        if i.get("filter_status") != "reject"
        and float(i.get("snooze_until") or 0) <= now
    ]
    return {"items": items}


@router.post("/api/agents/jira/clear-cache")
async def jira_clear_cache() -> dict:
    n = cursor_store.clear_cached("jira")
    await bus.publish("jira_cache_cleared", {"count": n})
    return {"cleared": n}


def merge_prev(payload: dict, prev: dict | None) -> dict:
    """Carry local state (done/snooze/linear) forward across re-polls, with one
    Jira-specific nuance: if the issue changed in Jira *after* Garth marked it
    done, drop handled_at so it resurfaces."""
    if not prev:
        return payload
    for k in _PRESERVED_KEYS:
        if k in prev:
            payload[k] = prev[k]
    if payload.get("handled_at"):
        updated = parse_iso(payload.get("date"))
        if updated and updated.timestamp() > float(payload["handled_at"]):
            payload.pop("handled_at", None)
    return payload


@router.post("/api/agents/jira/poll")
async def jira_poll(force: bool = False) -> dict:
    """Re-sync issues matching the enabled JQL scopes. Issues that left the
    result set (resolved/closed/reassigned) are evicted from cache.
    force=True wipes cache first."""
    if not _jira_configured():
        raise HTTPException(400, "Jira not authorized — JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN missing")

    fetcher = JiraFetcher()
    try:
        issues = await fetcher.fetch_all()
    except Exception as e:
        log.error("Jira fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Jira fetch failed: {type(e).__name__}: {e}")

    fetched_ids = {t.msg_id for t in issues}
    cached_payloads = {p["msg_id"]: p for p in cursor_store.list_cached("jira")}
    stale_ids = set(cached_payloads.keys()) - fetched_ids
    for sid in stale_ids:
        cursor_store.delete_cached("jira", sid)

    if force:
        cursor_store.clear_cached("jira")
        cached_payloads = {}

    items: list[dict] = []
    new_count = 0
    for t in issues:
        payload = t.summary()
        prev = cached_payloads.get(t.msg_id)
        if prev:
            merge_prev(payload, prev)
        else:
            new_count += 1
            await bus.publish("jira_item", payload)
        cursor_store.put_cached("jira", t.msg_id, payload)
        cursor_store.mark_seen("jira", t.msg_id)
        items.append(payload)

    summary = {
        "queried_total": len(issues),
        "new_count": new_count,
        "from_cache": len(issues) - new_count,
        "removed_stale": len(stale_ids),
        "items": items,
    }
    await bus.publish(
        "jira_poll_complete",
        {k: v for k, v in summary.items() if k != "items"},
    )
    return summary
