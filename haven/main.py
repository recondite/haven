import asyncio
import json
import logging
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("haven")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from haven import config, filters, scoring, store, wiki
from haven.db import cursor_store
from haven.events import bus
from haven.sources.gmail import GmailFetcher, GmailItem
from haven.sources.gmail_auth import GmailAuth

STATIC_DIR = Path(__file__).parent / "web" / "static"

gmail_auth = GmailAuth(
    client_id=config.GOOGLE_OAUTH_CLIENT_ID or "",
    client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET or "",
    redirect_uri=config.GOOGLE_OAUTH_REDIRECT_URI,
    token_path=config.GMAIL_TOKEN_PATH,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.ensure_dirs()
    wiki.ensure_wiki()
    heartbeat = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        heartbeat.cancel()


app = FastAPI(title="Haven", version="0.1.0", lifespan=lifespan)


async def _heartbeat_loop() -> None:
    """Emit a 2s heartbeat so the UI can show a live indicator until real agents start firing events."""
    i = 0
    while True:
        i += 1
        await bus.publish("heartbeat", {"n": i, "ts": time.time()})
        await asyncio.sleep(2)


# ─── API ─────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "ts": time.time(),
        "subscribers": bus.subscriber_count,
        "gmail_authed": gmail_auth.is_authed(),
    }


@app.get("/api/llm/status")
async def llm_status() -> dict:
    import os as _os
    from haven import llm
    node_pair = llm.node_entry_path()
    return {
        "cli_available": llm.cli_available(),
        "cli_path": llm.cli_path(),
        "node_direct": {
            "node_exe": node_pair[0] if node_pair else None,
            "cli_js": node_pair[1] if node_pair else None,
            "preferred": node_pair is not None,
        },
        "model": config.LLM_MODEL,
        "haven_claude_cli_env": _os.environ.get("HAVEN_CLAUDE_CLI"),
        "PATH": _os.environ.get("PATH", "")[:3000],
        "PATHEXT": _os.environ.get("PATHEXT", ""),
    }


@app.get("/api/llm/test")
@app.post("/api/llm/test")
async def llm_test() -> dict:
    """End-to-end test: spawn `claude --print` with a minimal prompt, verify it round-trips.

    Allows GET so it can be hit directly from the browser address bar.
    """
    from haven import llm
    try:
        raw = await llm.claude_call(
            'Reply with exactly this JSON and nothing else: {"hello": "world"}',
            timeout=30.0,
        )
        return {"ok": True, "response": raw[:1000]}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}


@app.get("/api/auth/gmail/status")
async def gmail_status() -> dict:
    return {
        "authed": gmail_auth.is_authed(),
        "scopes_ok": gmail_auth.has_required_scopes(),
    }


# ─── Gmail archive (per ground rules: label-removal only, never delete/trash) ───
def _gmail_service():
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    creds = gmail_auth.credentials()
    if creds is None:
        raise HTTPException(400, "Gmail not authorized")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        gmail_auth.token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


@app.post("/api/agents/gmail/items/{msg_id}/archive")
async def gmail_archive_one(msg_id: str) -> dict:
    """Archive a single message: remove the INBOX label. Non-destructive."""
    service = _gmail_service()

    def _do() -> dict:
        return (
            service.users()
            .messages()
            .modify(userId="me", id=msg_id, body={"removeLabelIds": ["INBOX"]})
            .execute()
        )

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        log.error("Archive %s failed: %s", msg_id, e)
        raise HTTPException(500, f"Archive failed: {e}")

    await bus.publish("gmail_item_archived", {"msg_id": msg_id})
    return {"archived": [msg_id]}


# ─── Watchlist (UI-managed keyword list) ─────────────────
@app.get("/api/agents/gmail/watchlist")
async def watchlist_get() -> dict:
    return {"keywords": filters.get_watchlist()}


@app.post("/api/agents/gmail/watchlist")
async def watchlist_add(payload: dict) -> dict:
    keyword = (payload.get("keyword") or "").strip()
    if not keyword:
        raise HTTPException(400, "keyword required")
    try:
        kws = filters.add_watchlist_keyword(keyword)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"keywords": kws}


@app.delete("/api/agents/gmail/watchlist/{keyword}")
async def watchlist_delete(keyword: str) -> dict:
    kws = filters.remove_watchlist_keyword(keyword)
    return {"keywords": kws}


@app.post("/api/agents/gmail/block")
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


@app.post("/api/agents/gmail/archive-noise")
async def gmail_archive_noise() -> dict:
    """Bulk-archive every cached item currently tagged 'noise'. Non-destructive."""
    items = cursor_store.list_cached("gmail")
    noise_ids = [i["msg_id"] for i in items if i.get("tag") == "noise"]
    if not noise_ids:
        return {"archived": [], "count": 0}

    service = _gmail_service()

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


# ─── Wiki ingest ─────────────────────────────────────────
@app.post("/api/wiki/ingest")
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


@app.get("/api/wiki/pages")
async def wiki_pages() -> dict:
    """List all wiki pages — useful for a future browse UI."""
    return {"pages": [str(p.relative_to(wiki.WIKI_DIR).as_posix()) for p in wiki.list_pages()]}


# ─── Gmail items ─────────────────────────────────────────
@app.get("/api/agents/gmail/items")
async def gmail_items() -> dict:
    """Return all cached Gmail items (most recent first), excluding pre-filtered ones.

    Used on page load to rehydrate the UI immediately without waiting for a poll.
    """
    return {
        "items": [
            i for i in cursor_store.list_cached("gmail")
            if i.get("filter_status") != "reject"
        ]
    }


@app.post("/api/agents/gmail/poll")
async def gmail_poll(force: bool = False) -> dict:
    """Poll Gmail and return the current matching set.

    The query — is:important is:unread in:inbox — defines the live AR view: what's still
    requiring Garth's attention right now. Items that drop out (read, archived, marked
    unimportant in Gmail) are automatically excluded from the next response.

    Per matching ID:
      - if cached and not `force`: reuse the cached enriched payload (cheap, no API call)
      - else: fetch full message + enrich + cache

    The response is the full current set, in the order Gmail returned them (recency desc).
    """
    if not gmail_auth.is_authed():
        raise HTTPException(400, "Gmail not authorized")

    # Queries come from agents/gmail.yaml (editable without code changes).
    cfg = filters.load_config()
    queries = cfg.get("queries") or ["is:important is:unread in:inbox"]
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

    # IDs that need fresh processing this turn — excludes already-cached and
    # previously-rejected items.
    to_process = [mid for mid in all_ids if mid not in cached and mid not in previously_rejected]

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
    for mid, meta in metadata_by_id.items():
        decision, reason, flags = filters.apply_filter(meta)
        if decision != filters.Decision.REJECT and filters.auto_approve_from_history(meta):
            decision = filters.Decision.ACCEPT
            flags = {**flags, "auto_approved_history": True}
            reason = "history: Garth has replied in this thread"
        if decision == filters.Decision.REJECT:
            cursor_store.mark_rejected("gmail", mid, reason)
            new_rejected += 1
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
        scores = await scoring.score_emails_concurrent(items_to_score, max_concurrent=5)
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

    summary = {
        "queried_total": len(all_ids),
        "new_count": new_count,
        "from_cache": sum(1 for mid in all_ids if mid in cached and cached[mid].get("filter_status") != "reject"),
        "previously_rejected": len(previously_rejected),
        "rejected_by_filter": new_rejected,
        "scored_by_llm": len(items_to_score),
        "labeled_haven": labeled_count,
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


@app.get("/api/sse/stream")
async def sse_stream():
    queue = bus.subscribe()

    async def gen():
        try:
            while True:
                event = await queue.get()
                yield {
                    "event": event["event"],
                    "data": json.dumps(event["data"], default=str),
                }
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(gen())


# ─── Gmail OAuth ─────────────────────────────────────────
@app.get("/oauth/authorize")
async def oauth_authorize() -> RedirectResponse:
    if not config.GOOGLE_OAUTH_CLIENT_ID or not config.GOOGLE_OAUTH_CLIENT_SECRET:
        raise HTTPException(500, "Google OAuth client not configured (.env missing keys)")
    url = gmail_auth.begin()
    return RedirectResponse(url, status_code=307)


@app.get("/oauth/callback")
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


# ─── Static UI (mounted last so /api/* and /oauth/* take precedence) ───
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
