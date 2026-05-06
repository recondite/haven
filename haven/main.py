import asyncio
import json
import logging
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import yaml

log = logging.getLogger("haven")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from haven import config, filters, linear, scoring, store, wiki
from haven.db import cursor_store
from haven.events import bus
from haven.sources.gmail import GmailFetcher, GmailItem
from haven.sources.gmail_auth import GmailAuth
from haven.sources.slack import SlackFetcher
from haven.sources.freshservice import FreshserviceFetcher

STATIC_DIR = Path(__file__).parent / "web" / "static"

gmail_auth = GmailAuth(
    client_id=config.GOOGLE_OAUTH_CLIENT_ID or "",
    client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET or "",
    redirect_uri=config.GOOGLE_OAUTH_REDIRECT_URI,
    token_path=config.GMAIL_TOKEN_PATH,
)


# ─── Phase 1.8: scheduled poller orchestrator ───────────
# Quiet hours (no automatic polls fire; manual "Poll now" still works).
# Local timezone of the server. Times are 24h.
QUIET_HOURS_START = 0   # midnight
QUIET_HOURS_END = 7     # 7 AM (exclusive — 07:00:00 is awake)


def _seconds_until_quiet_end() -> float:
    """If we're currently in quiet hours, return seconds to sleep until they end.
    Otherwise return 0.0 (proceed immediately)."""
    now = datetime.now()
    if QUIET_HOURS_START <= now.hour < QUIET_HOURS_END:
        target = now.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return (target - now).total_seconds()
    return 0.0


def _read_poll_seconds(yaml_name: str, default: int) -> tuple[int, bool]:
    """Read `poll_seconds` and `enabled` from `agents/<name>.yaml`. Defaults
    to (default, True) if the file/key is missing."""
    path = config.AGENTS_CONFIG_DIR / yaml_name
    if not path.exists():
        return default, True
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        secs = int(data.get("poll_seconds") or default)
        enabled = bool(data.get("enabled", True))
        return secs, enabled
    except Exception as e:
        log.warning("Failed to read %s: %s — using defaults", yaml_name, e)
        return default, True


async def _scheduled_poll_loop(name: str, poll_fn, poll_seconds: int) -> None:
    """Periodic background poller for a single source. Honors quiet hours.

    If a poll itself takes longer than `poll_seconds` (Gmail can take 5-8 min on
    cold cache), the next iteration sleeps `poll_seconds` AFTER the poll
    completes — never piles up overlapping polls.
    """
    log.info(
        "Scheduled poller [%s] started: every %ds (quiet hours %02d:00-%02d:00)",
        name, poll_seconds, QUIET_HOURS_START, QUIET_HOURS_END,
    )
    # Stagger initial polls so all three sources don't hammer simultaneously at startup.
    initial_delay = {"gmail": 5, "slack": 20, "freshservice": 35}.get(name, 15)
    await asyncio.sleep(initial_delay)
    while True:
        try:
            wait = _seconds_until_quiet_end()
            if wait > 0:
                log.info("Scheduled [%s]: in quiet hours, sleeping %.0fmin until 7am", name, wait / 60)
                await asyncio.sleep(wait)
                continue
            log.info("Scheduled [%s]: firing poll", name)
            await poll_fn()
        except asyncio.CancelledError:
            log.info("Scheduled poller [%s] cancelled", name)
            raise
        except Exception as e:
            log.error("Scheduled [%s] poll error: %s", name, e)
        await asyncio.sleep(poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.ensure_dirs()
    wiki.ensure_wiki()
    heartbeat = asyncio.create_task(_heartbeat_loop())

    # Spawn one background poller per source. Reads `poll_seconds` from each
    # agent yaml at startup; if a yaml has `enabled: false`, that source is
    # skipped (manual polling still works for it).
    schedulers: list[asyncio.Task] = []
    sources = [
        ("gmail", "gmail.yaml", 300, lambda: gmail_poll()),
        ("slack", "slack.yaml", 300, lambda: slack_poll()),
        ("freshservice", "freshservice.yaml", 3600, lambda: freshservice_poll()),
    ]
    for name, yaml_name, default_secs, poll_fn in sources:
        secs, enabled = _read_poll_seconds(yaml_name, default_secs)
        if not enabled:
            log.info("Scheduled [%s]: disabled in %s — skipping orchestrator", name, yaml_name)
            continue
        schedulers.append(asyncio.create_task(_scheduled_poll_loop(name, poll_fn, secs)))

    try:
        yield
    finally:
        heartbeat.cancel()
        for s in schedulers:
            s.cancel()


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


# ─── Linear AR capture (Phase 1.6, source-generic) ───────
@app.post("/api/items/{source}/{msg_id:path}/linear")
async def item_to_linear(source: str, msg_id: str) -> dict:
    """Source-generic AR capture — used by any agent (gmail, slack, freshservice).
    msg_id may contain ':' (e.g. slack channel:ts), hence path conversion.
    """
    if source not in {"gmail", "slack", "freshservice"}:
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
        issue = await linear.create_issue_from_email(item)  # works for any source — uses subject/sender/summary
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


# Back-compat: the original /api/agents/gmail/items/{id}/linear route still works.
@app.post("/api/agents/gmail/items/{msg_id}/linear")
async def gmail_to_linear(msg_id: str) -> dict:
    """Create a Linear issue in the CIO ARs project from a cached Gmail item.

    The 30-second undo lives entirely in the UI: the browser delays this POST
    by 30s and cancels client-side if the user clicks Undo. By the time we get
    here, the user has committed — we create the issue and never delete or
    archive it (per Haven ground rules).
    """
    cached = cursor_store.get_cached_payloads("gmail", [msg_id])
    item = cached.get(msg_id)
    if not item:
        raise HTTPException(404, f"msg_id {msg_id} not in cache")

    if item.get("linear_id"):
        return {
            "already_created": True,
            "linear_id": item["linear_id"],
            "linear_url": item.get("linear_url"),
            "linear_identifier": item.get("linear_identifier"),
        }

    try:
        issue = await linear.create_issue_from_email(item)
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
    cursor_store.put_cached("gmail", msg_id, item)

    await bus.publish(
        "gmail_linearized",
        {
            "msg_id": msg_id,
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


@app.post("/api/agents/gmail/clear-cache")
async def gmail_clear_cache() -> dict:
    """Wipe every cached Gmail payload AND every rejection marker so the next
    poll re-fetches and re-evaluates from scratch. Mirrors slack/freshservice."""
    cleared = cursor_store.clear_cached("gmail")
    rejections = cursor_store.clear_rejections("gmail")
    await bus.publish("gmail_cache_cleared", {"count": cleared})
    return {"cleared": cleared, "rejections_cleared": rejections}


@app.post("/api/agents/gmail/apply-noise-label")
async def gmail_apply_noise_label() -> dict:
    """One-shot: label every item Haven currently classifies as 'noise' in Gmail.

    Covers both:
      1. Cached items with `tag == "noise"` (LLM-tagged noise that survived filter)
      2. seen_items rows with `status == "rejected"` (pre-filter rejects)

    Useful after rule changes, or to retroactively apply the new noise-label
    behavior to items classified before the labeling code shipped.
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
    rejected_rows = cursor_store._conn.execute(
        "SELECT item_id FROM seen_items WHERE source = 'gmail' AND status = 'rejected'"
    ).fetchall()
    for (mid,) in rejected_rows:
        if mid:
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


@app.post("/api/agents/gmail/refilter-cached")
async def gmail_refilter_cached() -> dict:
    """Re-run `filters.apply_filter` against every cached Gmail item.

    Items that NOW match a never_keep / reject rule (because rules were tightened
    after they were originally cached) get marked rejected and dropped from the
    cache. Used to retroactively clean up items that predate a rule change —
    e.g., password-reset emails that survived because the never_keep regex was
    added later.

    Returns counts and the subjects of evicted items so it's auditable.
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
    noise_ids: set[str] = set()  # msg_ids to add the "noise" Gmail label to
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

    summary = {
        "queried_total": len(all_ids),
        "new_count": new_count,
        "from_cache": sum(1 for mid in all_ids if mid in cached and cached[mid].get("filter_status") != "reject"),
        "previously_rejected": len(previously_rejected),
        "rejected_by_filter": new_rejected,
        "scored_by_llm": len(items_to_score),
        "labeled_haven": labeled_count,
        "labeled_noise": labeled_noise,
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


# ─── Slack agent (Phase 2.0) ─────────────────────────────
@app.get("/api/auth/slack/status")
async def slack_status() -> dict:
    has_user = bool(config.SLACK_USER_TOKEN)
    has_bot = bool(config.SLACK_BOT_TOKEN)
    if not has_user:
        return {"authed": False, "reason": "SLACK_USER_TOKEN missing"}
    try:
        from haven.sources.slack import SlackClient
        client = SlackClient()
        uid = await client.self_user_id()
        return {"authed": True, "user_id": uid, "has_bot_token": has_bot}
    except Exception as e:
        return {"authed": False, "reason": str(e)}


@app.get("/api/agents/slack/items")
async def slack_items() -> dict:
    """Slack items are 'unread'-scoped, so cached payloads older than 30 min
    are likely stale (user may have read them). Return only fresh ones; the
    user should Poll to refresh."""
    cutoff = time.time() - 30 * 60
    fresh: list[dict] = []
    for i in cursor_store.list_cached("slack"):
        if i.get("filter_status") == "reject":
            continue
        try:
            if float(i.get("cached_at") or 0) < cutoff:
                continue
        except Exception:
            continue
        fresh.append(i)
    return {"items": fresh}


@app.post("/api/agents/slack/clear-cache")
async def slack_clear_cache() -> dict:
    """One-shot wipe of cached Slack payloads. Useful after changing the
    'what to fetch' rules (e.g. moving to unread-only)."""
    n = cursor_store.clear_cached("slack")
    await bus.publish("slack_cache_cleared", {"count": n})
    return {"cleared": n}


@app.delete("/api/agents/slack/items/{msg_id:path}")
async def slack_dismiss_item(msg_id: str) -> dict:
    """Remove a single Slack item from the local cache (user opened it in Slack)."""
    removed = cursor_store.delete_cached("slack", msg_id)
    return {"removed": removed, "msg_id": msg_id}


@app.post("/api/agents/slack/poll")
async def slack_poll(force: bool = False) -> dict:
    """Pull DMs, @mentions, watched channels, and watched-user messages.
    Each item is scored, cached, and SSE-emitted. Returns the current set."""
    if not config.SLACK_USER_TOKEN:
        raise HTTPException(400, "Slack not authorized — SLACK_USER_TOKEN missing in .env")

    fetcher = SlackFetcher()
    try:
        slack_items = await fetcher.fetch_all()
    except Exception as e:
        log.error("Slack fetch failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Slack fetch failed: {type(e).__name__}: {e}")

    all_ids = [s.msg_id for s in slack_items]
    if force:
        # Wipe the cache too — old payloads may correspond to messages that are
        # now read, and Slack semantics are "unread only" so we don't want to
        # reuse stale entries.
        wiped = cursor_store.clear_cached("slack")
        cursor_store.clear_rejections("slack")
        log.info("Slack force-poll: cleared %d cached payloads", wiped)
        cached: dict[str, dict] = {}
        previously_rejected: set[str] = set()
    else:
        cached = cursor_store.get_cached_payloads("slack", all_ids)
        previously_rejected = cursor_store.get_rejected_set("slack", all_ids)

    new_items: list = [s for s in slack_items if s.msg_id not in cached and s.msg_id not in previously_rejected]

    # Build summary payloads. Then score+cache+publish PER ITEM as scores
    # complete — that way the UI sees items stream in over the ~2min LLM run
    # instead of getting a wall of 20 at the end.
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
        # Split: DMs (1:1 IM and group MPIM) get a deterministic score — every
        # DM is a direct ask to Garth, and the LLM was burning ~6s per call to
        # land at "yes this DM needs a reply". Channel messages still go
        # through the LLM since #elt-2026 noise is harder to tag deterministically.
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

        sem = asyncio.Semaphore(5)

        async def _score_one(idx):
            s = new_items[idx]
            payload = new_payloads[idx]
            if payload.get("channel_type") in dm_types:
                # Skip LLM — apply deterministic DM score
                score = dict(DM_SCORE)
                # Use the snippet as a summary so it shows under the subject line.
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


# ─── Freshservice (Phase 2.1) ────────────────────────────
@app.get("/api/auth/freshservice/status")
async def freshservice_status() -> dict:
    if not config.FRESHSERVICE_API_KEY or not config.FRESHSERVICE_DOMAIN:
        return {"authed": False, "reason": "FRESHSERVICE_API_KEY or FRESHSERVICE_DOMAIN missing in .env"}
    try:
        from haven.sources.freshservice import FreshserviceClient, load_config as fs_load_config
        email = (fs_load_config().get("identity") or {}).get("email") or ""
        client = FreshserviceClient()
        agent_id = await client.self_agent_id(email or None)
        return {"authed": True, "agent_id": agent_id, "domain": config.FRESHSERVICE_DOMAIN}
    except Exception as e:
        return {"authed": False, "reason": str(e)}


@app.get("/api/agents/freshservice/items")
async def freshservice_items() -> dict:
    """Open tickets persist until next poll declares them closed — no freshness cutoff."""
    items = [i for i in cursor_store.list_cached("freshservice") if i.get("filter_status") != "reject"]
    return {"items": items}


@app.post("/api/agents/freshservice/clear-cache")
async def freshservice_clear_cache() -> dict:
    n = cursor_store.clear_cached("freshservice")
    await bus.publish("freshservice_cache_cleared", {"count": n})
    return {"cleared": n}


@app.post("/api/agents/freshservice/poll")
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
            for k in ("linear_id", "linear_url", "linear_identifier", "linear_created_at"):
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
