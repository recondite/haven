"""Gmail poll pipeline (Passes A–E), extracted from the route handler.

The query — is:important is:unread in:inbox — defines the live AR view: what's
still requiring Garth's attention right now. Items that drop out (read, archived,
marked unimportant in Gmail) are automatically excluded from the next response.

Per matching ID:
  - if cached and not `force`: reuse the cached enriched payload (cheap, no API call)
  - else: fetch full message + enrich + cache

Pass A  cheap metadata-only fetch for the filter decision (parallel, conc=10)
Pass B  apply deterministic filter on metadata; reject -> mark, never full-fetch
Pass C  full fetch + enrichment for survivors only (parallel, conc=5)
Pass D  LLM-score the survivors (parallel, conc=5, Haiku)
Pass E  build response, cache, label, and SSE-emit per new item
"""
from __future__ import annotations

import asyncio
import logging
import traceback

from fastapi import HTTPException

from haven import filters, scoring
from haven.db import cursor_store
from haven.deps import gmail_auth
from haven.events import bus
from haven.sources.gmail import GmailFetcher, GmailItem

log = logging.getLogger("haven")

# Hard ceiling on the enumerated id list (matches GmailFetcher.list_message_ids
# default). Set high so the unread set is enumerated to completion for any real
# inbox — the id list is cheap and completeness makes the cache reconcile
# reliable. Only if a poll actually returns this many do we skip the prune.
LIST_MAX_TOTAL = 2000

# Max NEW (uncached) items to fully fetch + score per poll. Enumeration can now
# return a large unread set (Gmail over-marks "important"); this bounds the
# expensive per-poll work to roughly the pre-pagination volume. Reconcile/prune
# still run over the COMPLETE set — only processing is capped.
PROCESS_CAP = 100


def resolved_ids(cached_items: list[dict], live_ids: list[str]) -> list[str]:
    """Cached Gmail ids that have left the live unread/important set (read,
    archived, or deprioritised in Gmail directly) and were NOT handled inside
    Haven — i.e. the ones to drop so Haven mirrors the inbox. Items with
    handled_at are retained (Hide-handled toggle / unmark). The caller must skip
    this when the live id list may be capped."""
    live = set(live_ids)
    return [it["msg_id"] for it in cached_items
            if it.get("msg_id") and it["msg_id"] not in live and not it.get("handled_at")]


async def run(force: bool = False) -> dict:
    """Poll Gmail and return the current matching set. Raises HTTPException on
    auth/list failure (preserved from the original route behavior)."""
    if not gmail_auth.is_authed():
        raise HTTPException(400, "Gmail not authorized")

    # Queries come from agents/gmail.yaml (editable without code changes).
    cfg = filters.load_config()
    queries = cfg.get("queries") or ["is:important is:unread in:inbox -label:ignore"]
    fetcher = GmailFetcher(auth=gmail_auth, queries=queries)

    try:
        all_ids = await fetcher.list_message_ids()
    except Exception as e:
        log.error("Gmail list failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Gmail list failed: {type(e).__name__}: {e}")

    # On force, wipe all rejection markers and the cached payloads so every item
    # is re-evaluated from scratch.
    if force:
        cursor_store.clear_rejections("gmail")
        cached: dict[str, dict] = {}
        previously_rejected: set[str] = set()
    else:
        cached = cursor_store.get_cached_payloads("gmail", all_ids)
        previously_rejected = cursor_store.get_rejected_set("gmail", all_ids)

    # Reconcile: the query IS the live AR set. A cached item whose id is no longer
    # in it has been read / archived / deprioritised in Gmail directly, so drop it
    # — Haven mirrors the inbox, it doesn't hoard resolved mail. Keep items Haven
    # itself handled (retained for the Hide-handled toggle + unmark). Skip only if
    # the id list hit its hard ceiling (can't tell "read" from "didn't fit").
    if not force and len(all_ids) < LIST_MAX_TOTAL:
        stale = resolved_ids(cursor_store.list_cached("gmail"), all_ids)
        for mid in stale:
            cursor_store.delete_cached("gmail", mid)
            await bus.publish("gmail_resolved", {"msg_id": mid})
        if stale:
            log.info("Gmail poll: pruned %d resolved item(s) no longer in the unread set", len(stale))

    # IDs that need fresh processing this turn — excludes already-cached and
    # previously-rejected items, then capped so a large unread set doesn't turn
    # one poll into hundreds of fetch+score calls (the tail is picked up next poll).
    to_process = [mid for mid in all_ids if mid not in cached and mid not in previously_rejected]
    to_process = to_process[:PROCESS_CAP]

    # Pass A: cheap metadata-only fetch for filter decision (parallel, conc=10).
    metadata_by_id: dict[str, dict] = {}
    errors: list[dict] = []
    if to_process:
        meta_sem = asyncio.Semaphore(10)

        async def _meta_one(mid: str) -> tuple[str, dict | None, str | None]:
            async with meta_sem:
                try:
                    return mid, await fetcher.fetch_metadata(mid), None
                except Exception as e:
                    return mid, None, str(e)

        log.info("metadata fetch: %d items, concurrency 10", len(to_process))
        meta_results = await asyncio.gather(*[_meta_one(m) for m in to_process])
        for mid, meta, err in meta_results:
            if err:
                log.error("Gmail metadata %s failed: %s", mid, err)
                errors.append({"msg_id": mid, "stage": "metadata", "error": err})
            elif meta is not None:
                metadata_by_id[mid] = meta

    # Pass B: apply filter on metadata. Reject -> mark in dedup, never full-fetch.
    new_rejected = 0
    survivor_ids: list[str] = []
    survivor_flags: dict[str, dict] = {}
    noise_ids: set[str] = set()  # msg_ids to add the "noise" Gmail label to
    travel_ids: set[str] = set()  # msg_ids to add the "travel" Gmail label to
    for mid, meta in metadata_by_id.items():
        decision, reason, flags = filters.apply_filter(meta)
        if decision != filters.Decision.REJECT and filters.auto_approve_from_history(meta):
            decision = filters.Decision.ACCEPT
            flags = {**flags, "auto_approved_history": True}
            reason = "history: Garth has replied in this thread"
        if decision == filters.Decision.REJECT:
            cursor_store.mark_rejected("gmail", mid, reason)
            new_rejected += 1
            noise_ids.add(mid)
        else:
            survivor_ids.append(mid)
            survivor_flags[mid] = flags

    # Pass C: full fetch + enrichment ONLY for survivors (parallel, conc=5).
    fetched_items: dict[str, GmailItem] = {}
    if survivor_ids:
        full_sem = asyncio.Semaphore(5)

        async def _full_one(mid: str) -> tuple[str, GmailItem | None, str | None]:
            async with full_sem:
                try:
                    return mid, await fetcher.fetch_message(mid), None
                except Exception as e:
                    return mid, None, str(e)

        log.info(
            "filter: %d new rejected, %d survivors -> full fetch",
            new_rejected,
            len(survivor_ids),
        )
        full_results = await asyncio.gather(*[_full_one(m) for m in survivor_ids])
        for mid, item, err in full_results:
            if err:
                log.error("Gmail full-fetch %s failed: %s", mid, err)
                errors.append({"msg_id": mid, "stage": "fetch", "error": err})
            elif item is not None:
                fetched_items[mid] = item

    # Pass D: LLM-score the survivors (parallel, conc=5, Haiku).
    items_to_score = list(fetched_items.values())
    scores: list[dict] = []
    if items_to_score:
        log.info("LLM scoring: %d items, concurrency 5", len(items_to_score))
        from haven import config
        scores = await scoring.score_emails_concurrent(items_to_score, max_concurrent=config.SCORE_CONCURRENCY)
    score_by_id = {item.msg_id: score for item, score in zip(items_to_score, scores)}

    # Pass E: build response — only items the user should see.
    items: list[dict] = []
    new_count = 0
    for mid in all_ids:
        if mid in cached:
            cached_payload = cached[mid]
            # Skip legacy rejected payloads from before metadata-first filtering.
            if cached_payload.get("filter_status") == "reject":
                continue
            items.append(cached_payload)
            continue
        if mid in previously_rejected:
            continue  # silent — never surface filtered items
        item = fetched_items.get(mid)
        if item is None:
            continue  # error in metadata or fetch stage

        payload = item.summary()
        flags = survivor_flags.get(mid, {})
        payload["filter_status"] = filters.Decision.ACCEPT if flags else filters.Decision.UNCERTAIN
        payload.update(flags)
        payload.update(score_by_id.get(mid, {}))

        # ELT/team senders get an urgency floor of "med" so they never get buried.
        if (flags.get("is_elt") or flags.get("is_team")) and payload.get("urgency") == "low":
            payload["urgency"] = "med"

        # Watchlist hit overrides LLM noise. Garth added the keyword specifically
        # because he cares about this sender/topic; if the LLM thinks otherwise,
        # promote the item so it's still visible.
        if flags.get("watchlist_match"):
            if payload.get("tag") == "noise":
                payload["tag"] = "fyi"
            if payload.get("urgency") == "low":
                payload["urgency"] = "med"

        # Priority approvals (e.g. Coupa) are pinned to tag="approval" and
        # urgency="urgent" regardless of LLM judgment — these block spend/workflow
        # and must never be buried.
        if flags.get("is_priority_approval"):
            payload["tag"] = "approval"
            payload["urgency"] = "urgent"
            payload["action_required"] = True

        # Travel notifications (flagged by the deterministic filter) are pinned
        # to tag="travel" regardless of what the LLM guessed — they're never
        # noise — and get the "travel" Gmail label below.
        if flags.get("is_travel"):
            payload["tag"] = "travel"
            travel_ids.add(mid)

        # LLM-tagged noise after the metadata filter passed — still apply the
        # Gmail "noise" label so it's filterable in Gmail itself.
        if payload.get("tag") == "noise":
            noise_ids.add(mid)
        cursor_store.put_cached("gmail", mid, payload)
        cursor_store.mark_seen("gmail", mid)
        items.append(payload)
        new_count += 1
        await bus.publish("gmail_item", payload)

    # Apply the "Haven" label to every newly-loaded item so they're sortable in
    # Gmail when not using this app. Best-effort — failures don't break the poll.
    labeled_count = 0
    if fetched_items:
        try:
            labeled_count = await fetcher.label_with_haven(list(fetched_items.keys()))
        except Exception as e:
            log.warning("Failed to apply Haven label: %s", e)

    # Apply the "noise" label to every item that got rejected by the pre-LLM
    # filter OR scored as tag=noise by the LLM. This makes them filterable in
    # Gmail (search: "label:noise") so Garth can bulk-archive without Haven.
    labeled_noise = 0
    if noise_ids:
        try:
            labeled_noise = await fetcher.label_messages(list(noise_ids), "noise")
            log.info("Applied 'noise' label to %d items", labeled_noise)
        except Exception as e:
            log.warning("Failed to apply noise label: %s", e)

    # Apply the "travel" label to airline/hotel/car-rental confirmations so they
    # are filterable in Gmail (search: "label:travel"). Best-effort.
    labeled_travel = 0
    if travel_ids:
        try:
            labeled_travel = await fetcher.label_messages(list(travel_ids), "travel")
            log.info("Applied 'travel' label to %d items", labeled_travel)
        except Exception as e:
            log.warning("Failed to apply travel label: %s", e)

    summary = {
        "queried_total": len(all_ids),
        "new_count": new_count,
        "from_cache": sum(1 for mid in all_ids if mid in cached and cached[mid].get("filter_status") != "reject"),
        "previously_rejected": len(previously_rejected),
        "rejected_by_filter": new_rejected,
        "scored_by_llm": len(items_to_score),
        "labeled_haven": labeled_count,
        "labeled_noise": labeled_noise,
        "labeled_travel": labeled_travel,
        "errors": errors,
        "items": items,
        "total_seen_all_time": cursor_store.seen_count("gmail"),
        "total_rejected_all_time": cursor_store.rejected_count("gmail"),
    }
    await bus.publish(
        "gmail_poll_complete",
        {k: v for k, v in summary.items() if k != "items"},
    )
    return summary
