"""Source-generic item actions: mark-done / snooze / Linear capture.

These work for any agent (gmail, slack, freshservice, otter). `msg_id` may
contain ':' (e.g. slack channel:ts), hence the {msg_id:path} converters.
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from haven import config, linear
from haven.db import cursor_store
from haven.events import bus
from haven.services import gmail_actions

log = logging.getLogger("haven")

router = APIRouter(prefix="/api/items", tags=["items"])


def _resolve_snooze_until(preset: str) -> float:
    """Translate a UI preset to an epoch-seconds deadline."""
    now = datetime.now()
    if preset == "1h":
        return (now + timedelta(hours=1)).timestamp()
    if preset == "tomorrow":
        target = (now + timedelta(days=1)).replace(
            hour=config.QUIET_HOURS_END, minute=0, second=0, microsecond=0
        )
        return target.timestamp()
    raise HTTPException(400, f"Unknown snooze preset: {preset}")


def _load_cached_or_404(source: str, msg_id: str) -> dict:
    if source not in config.KNOWN_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    cached = cursor_store.get_cached_payloads(source, [msg_id])
    item = cached.get(msg_id)
    if not item:
        raise HTTPException(404, f"{source}/{msg_id} not in cache")
    return item


async def capture_to_linear(source: str, msg_id: str) -> dict:
    """Create (or return existing) Linear issue for a cached item. Shared by the
    generic route and the Gmail back-compat route."""
    if source not in config.KNOWN_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")

    cached = cursor_store.get_cached_payloads(source, [msg_id])
    item = cached.get(msg_id)
    if not item:
        raise HTTPException(404, f"{source}/{msg_id} not in cache")

    if item.get("linear_id"):
        return {
            "already_created": True,
            "linear_id": item["linear_id"],
            "linear_url": item.get("linear_url"),
            "linear_identifier": item.get("linear_identifier"),
        }

    try:
        issue = await linear.create_issue_from_email(item)  # uses subject/sender/summary — any source
    except linear.LinearError as e:
        log.error("Linear create failed: %s", e)
        raise HTTPException(500, f"Linear create failed: {e}")
    except Exception as e:
        log.error("Linear create failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Linear create failed: {type(e).__name__}: {e}")

    item["linear_id"] = issue["id"]
    item["linear_url"] = issue["url"]
    item["linear_identifier"] = issue["identifier"]
    item["linear_created_at"] = time.time()
    cursor_store.put_cached(source, msg_id, item)

    await bus.publish(
        f"{source}_linearized",
        {
            "msg_id": msg_id,
            "source": source,
            "linear_id": issue["id"],
            "linear_url": issue["url"],
            "linear_identifier": issue["identifier"],
        },
    )
    return {
        "linear_id": issue["id"],
        "linear_url": issue["url"],
        "linear_identifier": issue["identifier"],
        "title": issue.get("title"),
        "priority": issue.get("priority"),
    }


@router.post("/gmail/thread/{thread_id}/mark-done")
async def gmail_thread_mark_done(thread_id: str) -> dict:
    """Mark an entire Gmail thread done in one click: archive (INBOX-label removal)
    every cached message in the thread and flag each handled, no matter which
    urgency bucket it landed in. Mirrors single-item mark-done but thread-wide.

    Defined before the generic /{source}/{msg_id:path}/mark-done route so the
    path-converter route doesn't capture "gmail/thread/<id>" as a msg_id.
    """
    items = cursor_store.get_cached_by_thread("gmail", thread_id)
    if not items:
        raise HTTPException(404, f"gmail thread {thread_id} not in cache")

    msg_ids = list(items.keys())
    await gmail_actions.archive_ids(msg_ids)  # one batchModify; hard-fails on error

    handled_at = time.time()
    for mid, item in items.items():
        item["handled_at"] = handled_at
        cursor_store.put_cached("gmail", mid, item)
        await bus.publish("gmail_handled", {"msg_id": mid, "handled_at": handled_at})

    return {
        "thread_id": thread_id,
        "handled": msg_ids,
        "count": len(msg_ids),
        "handled_at": handled_at,
        "archived_in_source": True,
    }


@router.post("/{source}/{msg_id:path}/mark-done")
async def item_mark_done(source: str, msg_id: str) -> dict:
    """Soft-mark an item as handled — drops it from hero + bucket. UI exposes
    a 'Hide handled' toggle to surface it back, plus an unmark-done endpoint.

    For Gmail items, also removes the INBOX label so Mark done = a single click
    that both flags the item as handled in Haven and clears it from your inbox.
    Hard-fails if the archive step errors (we don't claim "done" if Gmail isn't
    actually clean).
    """
    item = _load_cached_or_404(source, msg_id)
    archived_in_source = False
    if source == "gmail":
        await gmail_actions.archive_id(msg_id)
        archived_in_source = True
    item["handled_at"] = time.time()
    cursor_store.put_cached(source, msg_id, item)
    await bus.publish(f"{source}_handled", {"msg_id": msg_id, "handled_at": item["handled_at"]})
    return {
        "handled_at": item["handled_at"],
        "msg_id": msg_id,
        "archived_in_source": archived_in_source,
    }


@router.post("/{source}/{msg_id:path}/unmark-done")
async def item_unmark_done(source: str, msg_id: str) -> dict:
    item = _load_cached_or_404(source, msg_id)
    item.pop("handled_at", None)
    cursor_store.put_cached(source, msg_id, item)
    await bus.publish(f"{source}_unhandled", {"msg_id": msg_id})
    return {"unmarked": True, "msg_id": msg_id}


@router.post("/{source}/{msg_id:path}/snooze")
async def item_snooze(source: str, msg_id: str, payload: dict) -> dict:
    """Hide an item from the items endpoints until `snooze_until` passes.
    payload = {"preset": "1h" | "tomorrow"}."""
    preset = (payload.get("preset") or "").strip()
    item = _load_cached_or_404(source, msg_id)
    until = _resolve_snooze_until(preset)
    item["snooze_until"] = until
    cursor_store.put_cached(source, msg_id, item)
    await bus.publish(
        f"{source}_snoozed",
        {"msg_id": msg_id, "snooze_until": until, "preset": preset},
    )
    return {"snooze_until": until, "preset": preset, "msg_id": msg_id}


@router.post("/{source}/{msg_id:path}/unsnooze")
async def item_unsnooze(source: str, msg_id: str) -> dict:
    item = _load_cached_or_404(source, msg_id)
    item.pop("snooze_until", None)
    cursor_store.put_cached(source, msg_id, item)
    await bus.publish(f"{source}_unsnoozed", {"msg_id": msg_id})
    return {"unsnoozed": True, "msg_id": msg_id}


@router.post("/{source}/{msg_id:path}/linear")
async def item_to_linear(source: str, msg_id: str) -> dict:
    """Source-generic AR capture — used by any agent (gmail, slack, freshservice, otter)."""
    return await capture_to_linear(source, msg_id)


@router.post("/{source}/{msg_id:path}/linear/close")
async def item_close_linear(source: str, msg_id: str) -> dict:
    """Close the Linear issue linked to a cached item — transitions it to a
    completed ('Done') workflow state. Reversible in Linear; never deletes
    (per Ground Rules: status transitions on explicit user action are allowed).
    """
    item = _load_cached_or_404(source, msg_id)
    issue_id = item.get("linear_id")
    if not issue_id:
        raise HTTPException(400, f"{source}/{msg_id} has no linked Linear issue")

    try:
        issue = await linear.close_issue(issue_id)
    except linear.LinearError as e:
        log.error("Linear close failed: %s", e)
        raise HTTPException(500, f"Linear close failed: {e}")
    except Exception as e:
        log.error("Linear close failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Linear close failed: {type(e).__name__}: {e}")

    closed_at = time.time()
    state_name = (issue.get("state") or {}).get("name")
    item["linear_closed_at"] = closed_at
    item["linear_state"] = state_name
    cursor_store.put_cached(source, msg_id, item)

    await bus.publish(
        f"{source}_linear_closed",
        {
            "msg_id": msg_id,
            "source": source,
            "closed_at": closed_at,
            "linear_identifier": issue.get("identifier"),
            "state_name": state_name,
        },
    )
    return {
        "closed_at": closed_at,
        "msg_id": msg_id,
        "linear_identifier": issue.get("identifier"),
        "linear_url": issue.get("url"),
        "state_name": state_name,
    }
