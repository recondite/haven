"""Gmail agent routes: items, poll, cache, archive, block, watchlist, labels,
plus the Gmail OAuth flow."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from haven import config, filters
from haven.db import cursor_store
from haven.deps import gmail_auth
from haven.events import bus
from haven.routers.items import capture_to_linear
from haven.services import gmail_actions
from haven.services import gmail_poll as gmail_poll_service
from haven.sources.gmail import GmailFetcher

log = logging.getLogger("haven")

router = APIRouter(tags=["gmail"])


# ─── Auth status ─────────────────────────────────────────
@router.get("/api/auth/gmail/status")
async def gmail_status() -> dict:
    return {
        "authed": gmail_auth.is_authed(),
        "scopes_ok": gmail_auth.has_required_scopes(),
    }


# ─── Archive (label-removal only, never delete/trash) ────
@router.post("/api/agents/gmail/items/{msg_id}/archive")
async def gmail_archive_one(msg_id: str) -> dict:
    """Archive a single message: remove the INBOX label. Non-destructive."""
    await gmail_actions.archive_id(msg_id)
    return {"archived": [msg_id]}


# ─── Watchlist (UI-managed keyword list) ─────────────────
@router.get("/api/agents/gmail/watchlist")
async def watchlist_get() -> dict:
    return {"keywords": filters.get_watchlist()}


@router.post("/api/agents/gmail/watchlist")
async def watchlist_add(payload: dict) -> dict:
    keyword = (payload.get("keyword") or "").strip()
    if not keyword:
        raise HTTPException(400, "keyword required")
    try:
        kws = filters.add_watchlist_keyword(keyword)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"keywords": kws}


@router.delete("/api/agents/gmail/watchlist/{keyword}")
async def watchlist_delete(keyword: str) -> dict:
    kws = filters.remove_watchlist_keyword(keyword)
    return {"keywords": kws}


@router.post("/api/agents/gmail/block")
async def gmail_block(payload: dict) -> dict:
    """Add an email's sender to the dynamic block list and remove the item.

    payload = { "msg_id": "...", "domain_too": false }
    """
    msg_id = payload.get("msg_id")
    domain_too = bool(payload.get("domain_too", False))
    if not msg_id:
        raise HTTPException(400, "msg_id required")

    cached = cursor_store.get_cached_payloads("gmail", [msg_id])
    item = cached.get(msg_id)
    if not item:
        raise HTTPException(404, f"msg_id {msg_id} not in cache")

    sender = (item.get("sender_email") or "").strip().lower()
    if not sender:
        raise HTTPException(400, "no sender_email on cached item")

    try:
        filters.block_sender(
            sender,
            domain_too=domain_too,
            reason=f"blocked from msg {msg_id}",
        )
    except Exception as e:
        log.error("Block sender failed: %s", e)
        raise HTTPException(500, f"Block failed: {e}")

    # Mark this specific item rejected so it never returns either.
    cursor_store.mark_rejected("gmail", msg_id, f"blocked sender {sender}")

    await bus.publish(
        "gmail_blocked",
        {"msg_id": msg_id, "sender": sender, "domain_too": domain_too},
    )
    return {"blocked": sender, "msg_id": msg_id, "domain_blocked": domain_too}


@router.post("/api/agents/gmail/archive-noise")
async def gmail_archive_noise() -> dict:
    """Bulk-archive every cached item currently tagged 'noise'. Non-destructive."""
    items = cursor_store.list_cached("gmail")
    noise_ids = [i["msg_id"] for i in items if i.get("tag") == "noise"]
    if not noise_ids:
        return {"archived": [], "count": 0}

    service = await gmail_actions.gmail_service()

    def _do() -> dict:
        return (
            service.users()
            .messages()
            .batchModify(
                userId="me",
                body={"ids": noise_ids, "removeLabelIds": ["INBOX"]},
            )
            .execute()
        )

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        log.error("Bulk archive failed: %s", e)
        raise HTTPException(500, f"Bulk archive failed: {e}")

    for mid in noise_ids:
        await bus.publish("gmail_item_archived", {"msg_id": mid})
    return {"archived": noise_ids, "count": len(noise_ids)}


# ─── Items ───────────────────────────────────────────────
@router.get("/api/agents/gmail/items")
async def gmail_items() -> dict:
    """Return all cached Gmail items (most recent first), excluding pre-filtered ones.

    Used on page load to rehydrate the UI immediately without waiting for a poll.
    Filtered out: rejected, snoozed. Handled + Linear-captured items are returned
    and filtered client-side by the Hide handled toggle.
    """
    now = time.time()
    return {
        "items": [
            i for i in cursor_store.list_cached("gmail")
            if i.get("filter_status") != "reject"
            and float(i.get("snooze_until") or 0) <= now
        ]
    }


@router.post("/api/agents/gmail/clear-cache")
async def gmail_clear_cache() -> dict:
    """Wipe every cached Gmail payload AND every rejection marker so the next
    poll re-fetches and re-evaluates from scratch. Mirrors slack/freshservice."""
    cleared = cursor_store.clear_cached("gmail")
    rejections = cursor_store.clear_rejections("gmail")
    await bus.publish("gmail_cache_cleared", {"count": cleared})
    return {"cleared": cleared, "rejections_cleared": rejections}


@router.post("/api/agents/gmail/apply-noise-label")
async def gmail_apply_noise_label() -> dict:
    """One-shot: label every item Haven currently classifies as 'noise' in Gmail.

    Covers both:
      1. Cached items with `tag == "noise"` (LLM-tagged noise that survived filter)
      2. seen_items rows with `status == "rejected"` (pre-filter rejects)
    """
    if not gmail_auth.is_authed():
        raise HTTPException(400, "Gmail not authorized")

    noise_ids: set[str] = set()
    # 1. LLM-tagged noise in cache
    for item in cursor_store.list_cached("gmail"):
        if item.get("tag") == "noise":
            mid = item.get("msg_id") or ""
            if mid:
                noise_ids.add(mid)
    # 2. Pre-filter rejects (seen_items.status == 'rejected')
    for mid in cursor_store.list_rejected_ids("gmail"):
        noise_ids.add(mid)

    if not noise_ids:
        return {"labeled": 0, "candidates": 0}

    cfg = filters.load_config()
    queries = cfg.get("queries") or ["is:important is:unread in:inbox"]
    fetcher = GmailFetcher(auth=gmail_auth, queries=queries)
    try:
        labeled = await fetcher.label_messages(list(noise_ids), "noise")
    except Exception as e:
        log.error("apply-noise-label failed: %s", e)
        raise HTTPException(500, f"Labeling failed: {type(e).__name__}: {e}")
    log.info("apply-noise-label: labeled %d items", labeled)
    return {"labeled": labeled, "candidates": len(noise_ids)}


@router.post("/api/agents/gmail/refilter-cached")
async def gmail_refilter_cached() -> dict:
    """Re-run `filters.apply_filter` against every cached Gmail item.

    Items that NOW match a never_keep / reject rule (because rules were tightened
    after they were originally cached) get marked rejected and dropped from the
    cache. Returns counts and the subjects of evicted items so it's auditable.
    """
    evicted: list[dict] = []
    kept = 0
    for item in cursor_store.list_cached("gmail"):
        if item.get("filter_status") == "reject":
            continue
        decision, reason, _flags = filters.apply_filter(item)
        if decision == filters.Decision.REJECT:
            mid = item.get("msg_id") or ""
            cursor_store.mark_rejected("gmail", mid, reason)
            cursor_store.delete_cached("gmail", mid)
            evicted.append({"msg_id": mid, "subject": item.get("subject", ""), "reason": reason})
        else:
            kept += 1
    log.info("Gmail refilter: evicted %d, kept %d", len(evicted), kept)

    # Apply the "noise" Gmail label to every evicted msg so it's filterable in
    # Gmail itself ("label:noise" search). Best-effort — failures don't break.
    labeled_noise = 0
    if evicted and gmail_auth.is_authed():
        try:
            cfg = filters.load_config()
            queries = cfg.get("queries") or ["is:important is:unread in:inbox"]
            fetcher = GmailFetcher(auth=gmail_auth, queries=queries)
            labeled_noise = await fetcher.label_messages([e["msg_id"] for e in evicted], "noise")
            log.info("Refilter applied 'noise' label to %d evicted items", labeled_noise)
        except Exception as e:
            log.warning("Refilter failed to apply noise label: %s", e)

    return {"evicted": len(evicted), "kept": kept, "labeled_noise": labeled_noise, "items": evicted}


@router.post("/api/agents/gmail/poll")
async def gmail_poll(force: bool = False) -> dict:
    """Poll Gmail and return the current matching set. Thin wrapper over the
    extracted pipeline in services/gmail_poll.py."""
    return await gmail_poll_service.run(force)


# Back-compat: the original Gmail-specific Linear route. Delegates to the
# source-generic capture so there's a single implementation.
@router.post("/api/agents/gmail/items/{msg_id}/linear")
async def gmail_to_linear(msg_id: str) -> dict:
    """Create a Linear issue in the CIO ARs project from a cached Gmail item.

    The 30-second undo lives entirely in the UI: the browser delays this POST by
    30s and cancels client-side if the user clicks Undo. By the time we get here
    the user has committed — we create the issue and never delete or archive it.
    """
    return await capture_to_linear("gmail", msg_id)


# ─── Gmail OAuth ─────────────────────────────────────────
@router.get("/oauth/authorize")
async def oauth_authorize() -> RedirectResponse:
    if not config.GOOGLE_OAUTH_CLIENT_ID or not config.GOOGLE_OAUTH_CLIENT_SECRET:
        raise HTTPException(500, "Google OAuth client not configured (.env missing keys)")
    url = gmail_auth.begin()
    return RedirectResponse(url, status_code=307)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    if error:
        raise HTTPException(400, f"OAuth error from Google: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing code or state in callback")
    try:
        gmail_auth.complete(str(request.url), state)
    except Exception as e:
        raise HTTPException(400, f"OAuth completion failed: {e}")
    await bus.publish("auth_changed", {"source": "gmail", "authed": True})
    return RedirectResponse("/?gmail=connected", status_code=303)
