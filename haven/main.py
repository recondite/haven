"""Haven app assembly: FastAPI app, background scheduler, heartbeat, SSE, and
the small set of cross-cutting endpoints (health, llm status/test, favicon).

All per-source endpoints live in haven/routers/*. The Gmail poll pipeline lives
in haven/services/gmail_poll.py.
"""
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

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from haven import config
from haven.deps import gmail_auth
from haven.events import bus
from haven.routers import (
    contacts,
    dispatch,
    docs,
    evals,
    freshservice,
    gmail,
    identity,
    items,
    knowledge,
    otter,
    slack,
    spine,
    system,
    wiki,
)

STATIC_DIR = Path(__file__).parent / "web" / "static"


# ─── Scheduled poller orchestrator ───────────────────────
def _seconds_until_quiet_end() -> float:
    """If we're currently in quiet hours, return seconds to sleep until they end.
    Otherwise return 0.0 (proceed immediately)."""
    now = datetime.now()
    if config.QUIET_HOURS_START <= now.hour < config.QUIET_HOURS_END:
        target = now.replace(hour=config.QUIET_HOURS_END, minute=0, second=0, microsecond=0)
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
        name, poll_seconds, config.QUIET_HOURS_START, config.QUIET_HOURS_END,
    )
    # Stagger initial polls so all sources don't hammer simultaneously at startup.
    initial_delay = {"gmail": 5, "slack": 20, "freshservice": 35, "otter": 50}.get(name, 15)
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
    from haven import executor, store, wiki as wiki_mod
    store.ensure_dirs()
    wiki_mod.ensure_wiki()
    # M0.3: live send on an unsafe posture (non-localhost + no auth) is forced
    # dry before anything can fire. Audited; surfaced on /system and the UI.
    executor.enforce_boot_tripwire()
    heartbeat = asyncio.create_task(_heartbeat_loop())

    # Spawn one background poller per source. Reads `poll_seconds` from each
    # agent yaml at startup; if a yaml has `enabled: false`, that source is
    # skipped (manual polling still works for it).
    schedulers: list[asyncio.Task] = []
    sources = [
        ("gmail", "gmail.yaml", 300, lambda: gmail.gmail_poll()),
        ("slack", "slack.yaml", 300, lambda: slack.slack_poll()),
        ("freshservice", "freshservice.yaml", 3600, lambda: freshservice.freshservice_poll()),
        ("otter", "otter.yaml", 1800, lambda: otter.otter_poll()),
    ]
    for name, yaml_name, default_secs, poll_fn in sources:
        secs, enabled = _read_poll_seconds(yaml_name, default_secs)
        if not enabled:
            log.info("Scheduled [%s]: disabled in %s — skipping orchestrator", name, yaml_name)
            continue
        schedulers.append(asyncio.create_task(_scheduled_poll_loop(name, poll_fn, secs)))

    # Weekly roster-drift check (plan v4 Phase 3): refresh roster + ids, report
    # drift. Proposes only — writes nothing. Right-sized, not a live engine.
    async def _weekly_drift() -> None:
        from haven import identity
        await asyncio.sleep(120)  # let startup polls settle
        while True:
            try:
                await identity.scheduled_drift()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("weekly drift error: %s", e)
            await asyncio.sleep(7 * 24 * 3600)
    schedulers.append(asyncio.create_task(_weekly_drift()))

    # Nightly snapshot of the SQLite stores (M0.2). Runs once at startup (so a
    # backup exists from day one), then daily; idempotent per calendar day.
    async def _nightly_backup() -> None:
        from haven import backup as backup_mod
        await asyncio.sleep(60)
        while True:
            try:
                results = backup_mod.backup_now()
                created = [r["file"] for r in results if r.get("status") == "created"]
                if created:
                    log.info("Backup created: %s", ", ".join(created))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("nightly backup error: %s", e)
            await asyncio.sleep(24 * 3600)
    schedulers.append(asyncio.create_task(_nightly_backup()))

    try:
        yield
    finally:
        heartbeat.cancel()
        for s in schedulers:
            s.cancel()


app = FastAPI(title="Haven", version="0.1.0", lifespan=lifespan)


# ─── Auth ────────────────────────────────────────────────
# Bearer/Basic token on every endpoint. Basic makes the browser show a native
# login prompt and cache the creds (sent on fetch + EventSource same-origin, no
# login page needed). Bearer is for API/CLI clients. Disabled if no token set.
def _authorized(request) -> bool:
    import base64
    import hmac

    token = config.HAVEN_AUTH_TOKEN
    header = request.headers.get("authorization", "")
    scheme, _, cred = header.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        return hmac.compare_digest(cred, token)
    if scheme == "basic":
        try:
            _, _, pw = base64.b64decode(cred).decode("utf-8", "replace").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw, token)
    return False


@app.middleware("http")
async def require_auth(request, call_next):
    # Read the token live so tests/.env reloads take effect without reimport.
    if not config.HAVEN_AUTH_TOKEN or _authorized(request):
        return await call_next(request)
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Haven"'},
    )


async def _heartbeat_loop() -> None:
    """Emit a 2s heartbeat so the UI can show a live indicator until real agents start firing events."""
    i = 0
    while True:
        i += 1
        await bus.publish("heartbeat", {"n": i, "ts": time.time()})
        await asyncio.sleep(2)


# ─── Favicon (silences the cold-load /favicon.ico 404 in logs) ───
_FAVICON_SVG = (
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    b"<rect width='32' height='32' rx='7' fill='#5e6ad2'/>"
    b"<text x='16' y='22' text-anchor='middle' "
    b"font-family='-apple-system,Segoe UI,Roboto,sans-serif' "
    b"font-weight='700' font-size='18' fill='white'>H</text></svg>"
)


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ─── Cross-cutting API ───────────────────────────────────
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
    from haven import llm
    node_pair = llm.node_entry_path()
    is_local = config.LLM_MODE == "local"
    # "available" reflects the ACTIVE runtime: CLI resolvable for cli mode, or
    # the local endpoint actually answering /models for local mode.
    if is_local:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(config.LOCAL_LLM_BASE_URL.rstrip("/") + "/models")
            available = r.status_code == 200
        except Exception:
            available = False
    else:
        available = llm.cli_available()
    return {
        "cli_available": llm.cli_available(),
        "cli_path": llm.cli_path(),
        "node_direct": {
            "node_exe": node_pair[0] if node_pair else None,
            "cli_js": node_pair[1] if node_pair else None,
            "preferred": node_pair is not None,
        },
        "available": available,
        "model": config.LOCAL_LLM_MODEL if is_local else config.LLM_MODEL,
        "runtime": config.LLM_MODE,
        "local_base_url": config.LOCAL_LLM_BASE_URL if is_local else None,
    }


@app.get("/api/llm/test")
@app.post("/api/llm/test")
async def llm_test() -> dict:
    """End-to-end test of the ACTIVE runtime (claude CLI or local endpoint):
    send a minimal prompt, verify it round-trips.

    Allows GET so it can be hit directly from the browser address bar.
    """
    from haven import runtime
    try:
        raw = await runtime.call(
            'Reply with exactly this JSON and nothing else: {"hello": "world"}',
            timeout=60.0,
        )
        return {"ok": True, "runtime": config.LLM_MODE, "response": raw[:1000]}
    except Exception as e:
        return {"ok": False, "runtime": config.LLM_MODE, "error": str(e), "trace": traceback.format_exc()}


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


# ─── Routers ─────────────────────────────────────────────
app.include_router(gmail.router)
app.include_router(slack.router)
app.include_router(freshservice.router)
app.include_router(otter.router)
app.include_router(wiki.router)
app.include_router(contacts.router)
app.include_router(items.router)
app.include_router(spine.router)
app.include_router(dispatch.router)
app.include_router(identity.router)
app.include_router(evals.router)
app.include_router(knowledge.router)
app.include_router(docs.router)
app.include_router(system.router)


# ─── Static UI (mounted last so /api/* and /oauth/* take precedence) ───
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
