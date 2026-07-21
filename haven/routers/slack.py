"""Slack agent routes: status, items, cache, poll."""
from __future__ import annotations

import asyncio
import logging
import time
import traceback

from fastapi import APIRouter, HTTPException

from haven import config, scoring
from haven.db import cursor_store
from haven.events import bus
from haven.sources.slack import SlackFetcher

log = logging.getLogger("haven")

router = APIRouter(tags=["slack"])


@router.get("/api/auth/slack/status")
async def slack_status() -> dict:
    has_user = bool(config.SLACK_USER_TOKEN)
    has_bot = bool(config.SLACK_BOT_TOKEN)
    if not has_user:
        return {"authed": False, "reason": "SLACK_USER_TOKEN missing"}
    from haven.sources.slack import SlackClient
    client = SlackClient()
    try:
        uid = await client.self_user_id()
        return {"authed": True, "user_id": uid, "has_bot_token": has_bot}
    except Exception as e:
        return {"authed": False, "reason": str(e)}
    finally:
        await client.aclose()


@router.get("/api/agents/slack/items")
async def slack_items() -> dict:
    """Slack items are 'unread'-scoped, so cached payloads older than 30 min are
    likely stale (user may have read them). Return only fresh ones."""
    cutoff = time.time() - 30 * 60
    now = time.time()
    fresh: list[dict] = []
    for i in cursor_store.list_cached("slack"):
        if i.get("filter_status") == "reject":
            continue
        if float(i.get("snooze_until") or 0) > now:
            continue
        try:
            if float(i.get("cached_at") or 0) < cutoff:
                continue
        except Exception:
            continue
        fresh.append(i)
    return {"items": fresh}


@router.post("/api/agents/slack/clear-cache")
async def slack_clear_cache() -> dict:
    """One-shot wipe of cached Slack payloads."""
    n = cursor_store.clear_cached("slack")
    await bus.publish("slack_cache_cleared", {"count": n})
    return {"cleared": n}


@router.delete("/api/agents/slack/items/{msg_id:path}")
async def slack_dismiss_item(msg_id: str) -> dict:
    """Remove a single Slack item from the local cache (user opened it in Slack)."""
    removed = cursor_store.delete_cached("slack", msg_id)
    return {"removed": removed, "msg_id": msg_id}


@router.post("/api/agents/slack/poll")
async def slack_poll(force: bool = False) -> dict:
    """Pull DMs, @mentions, watched channels, and watched-user messages.
    Each item is scored, cached, and SSE-emitted. Returns the current set."""
    if not config.SLACK_USER_TOKEN:
        raise HTTPException(400, "Slack not authorized — SLACK_USER_TOKEN missing in .env")

    fetcher = SlackFetcher()
    if force:
        # Re-scan the full lookback: drop the DM search floor before fetching so
        # fetch_dms doesn't start from the recent steady-state cursor.
        cursor_store.set_cursor("slack", "dm_search_floor", "")
    try:
        slack_msgs = await fetcher.fetch_all()
    except Exception as e:
        log.error("Slack fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Slack fetch failed: {type(e).__name__}: {e}")

    all_ids = [s.msg_id for s in slack_msgs]
    if force:
        # Wipe the cache too — old payloads may correspond to messages that are
        # now read, and Slack semantics are "unread only".
        wiped = cursor_store.clear_cached("slack")
        cursor_store.clear_rejections("slack")
        log.info("Slack force-poll: cleared %d cached payloads", wiped)
        cached: dict[str, dict] = {}
        previously_rejected: set[str] = set()
    else:
        cached = cursor_store.get_cached_payloads("slack", all_ids)
        previously_rejected = cursor_store.get_rejected_set("slack", all_ids)

    new_items: list = [s for s in slack_msgs if s.msg_id not in cached and s.msg_id not in previously_rejected]

    # Build summary payloads. Then score+cache+publish PER ITEM as scores
    # complete — that way the UI sees items stream in over the ~2min LLM run.
    new_payloads = [s.summary() for s in new_items]

    cfg = fetcher.cfg.get("scoring") or {}
    elt_floor = cfg.get("elt_urgency_floor", "med")
    mark_floor = cfg.get("mark_urgency_floor", "med")
    URG_RANK = {"low": 0, "med": 1, "high": 2, "urgent": 3}

    def bump(urg: str, floor: str) -> str:
        if URG_RANK.get(urg, 0) < URG_RANK.get(floor, 0):
            return floor
        return urg

    items: list[dict] = []
    new_count = 0

    if new_payloads:
        # DMs (1:1 IM and group MPIM) get a deterministic score — every DM is a
        # direct ask to Garth. Channel messages still go through the LLM.
        dm_types = {"im", "mpim"}
        channel_indices = [i for i, p in enumerate(new_payloads) if p.get("channel_type") not in dm_types]
        dm_indices = [i for i, p in enumerate(new_payloads) if p.get("channel_type") in dm_types]
        log.info(
            "Slack scoring: %d items (%d channel via LLM, %d DM deterministic)",
            len(new_payloads), len(channel_indices), len(dm_indices),
        )

        DM_SCORE = {
            "tag": "action",
            "urgency": "med",
            "action_required": True,
            "reply_needed": True,
            "reply_reason": "Direct message to Garth — assume reply needed",
            "summary": "",
            "suggested_action": "Reply",
            "suggested_reply": "",
        }

        sem = asyncio.Semaphore(config.SCORE_CONCURRENCY)  # serialized on local engines

        async def _score_one(idx):
            s = new_items[idx]
            payload = new_payloads[idx]
            if payload.get("channel_type") in dm_types:
                # Skip LLM — apply deterministic DM score
                score = dict(DM_SCORE)
                score["summary"] = (payload.get("snippet") or "")[:200]
            else:
                async with sem:
                    score = await scoring.score_slack(payload)
            payload.update(score)
            if payload.get("is_watched_channel") and payload.get("urgency") == "low":
                payload["urgency"] = elt_floor
            if payload.get("is_watched_user"):
                payload["urgency"] = bump(payload.get("urgency", "low"), mark_floor)
            cursor_store.put_cached("slack", s.msg_id, payload)
            cursor_store.mark_seen("slack", s.msg_id)
            # Publish as soon as this one's done — SSE flushes to browser immediately.
            await bus.publish("slack_item", payload)
            return payload

        scored_payloads = await asyncio.gather(*[_score_one(i) for i in range(len(new_payloads))])
        items.extend(scored_payloads)
        new_count = len(scored_payloads)

    # Include cached items (still relevant) in the response
    for mid in all_ids:
        if mid in cached:
            items.append(cached[mid])

    summary = {
        "queried_total": len(all_ids),
        "new_count": new_count,
        "from_cache": len([m for m in all_ids if m in cached]),
        "previously_rejected": len(previously_rejected),
        "scored_by_llm": len(new_payloads),
        "items": items,
    }
    await bus.publish(
        "slack_poll_complete",
        {k: v for k, v in summary.items() if k != "items"},
    )
    return summary
