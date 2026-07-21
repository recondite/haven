"""LLM command bar: natural language -> {action, filter} plan over cached items.

Flow is always plan → preview → confirm → execute (never auto-run):
  POST /api/command/plan     {text}                      -> {action, filter, summary, matches, count}
  POST /api/command/execute  {action, items:[{source,msg_id}]} -> {done}

The LLM ONLY translates language into a structured filter + verb; it never
touches mail. `done` mirrors into Gmail (read + archive + Haven/Done label) via
the shared gmail_actions path, matching the Mark-done button.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from fastapi import APIRouter, HTTPException

from haven import config, runtime
from haven.config import KNOWN_SOURCES
from haven.db import cursor_store
from haven.events import bus
from haven.services import gmail_actions

router = APIRouter(prefix="/api/command", tags=["command"])
log = logging.getLogger("haven")

VALID_ACTIONS = {"search", "done"}
VALID_TAGS = {"approval", "action", "fyi", "travel", "noise"}
VALID_URGENCY = {"urgent", "high", "med", "low"}

PLAN_PROMPT = """You convert a natural-language inbox command into a STRICT JSON plan.
Return ONLY a JSON object: {{"action": ..., "filter": {{...}}, "summary": ...}}

action: "search" (just find/show matching items, change nothing) or
        "done" (mark the matched items done: read + archived in Gmail).

filter fields (include only the ones the command implies; omit or null the rest):
  source: one of gmail | slack | freshservice | otter  (null = all sources)
  sender_contains: substring to match against sender name/email/domain
  subject_contains: substring to match against the subject
  older_than_days: number  (item is older than N days)
  newer_than_days: number  (item is newer than N days)
  tag: one of approval | action | fyi | travel | noise
  urgency: one of urgent | high | med | low
  watchlist: true to require a watchlist keyword hit
  unhandled_only: true (default) skips already-handled items; false includes them

summary: one short sentence stating exactly what will happen.

Examples:
Command: Archive all older than 2 days
{{"action":"done","filter":{{"older_than_days":2,"unhandled_only":true}},"summary":"Mark done (read + archive) every unhandled item older than 2 days."}}
Command: find everything from Coupa
{{"action":"search","filter":{{"sender_contains":"coupa"}},"summary":"Show all items from Coupa."}}
Command: mark all slack noise done
{{"action":"done","filter":{{"source":"slack","tag":"noise","unhandled_only":true}},"summary":"Mark done all Slack items tagged noise."}}

Command: {text}
"""


def _age_days(payload: dict) -> float | None:
    """Age of an item in days from its `date` header (fallback `cached_at`)."""
    raw = payload.get("date")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        except (TypeError, ValueError):
            pass
    ca = payload.get("cached_at")
    if ca:
        try:
            return (time.time() - float(ca)) / 86400.0
        except (TypeError, ValueError):
            pass
    return None


def match_item(payload: dict, f: dict) -> bool:
    """Pure predicate: does a cached item match the plan's filter? Unhandled-only
    by default so a re-run doesn't re-touch items already done."""
    if f.get("unhandled_only", True) and payload.get("handled_at"):
        return False
    tag = f.get("tag")
    if tag and (payload.get("tag") or "").lower() != tag:
        return False
    urg = f.get("urgency")
    if urg and (payload.get("urgency") or "").lower() != urg:
        return False
    if f.get("watchlist") and not payload.get("watchlist_match"):
        return False
    sc = f.get("sender_contains")
    if sc:
        blob = " ".join(str(payload.get(k) or "") for k in
                        ("sender_name", "sender_email", "sender_domain", "channel_name")).lower()
        if sc.lower() not in blob:
            return False
    subj = f.get("subject_contains")
    if subj and subj.lower() not in (payload.get("subject") or "").lower():
        return False
    age = _age_days(payload)
    if f.get("older_than_days") is not None and (age is None or age < f["older_than_days"]):
        return False
    if f.get("newer_than_days") is not None and (age is None or age > f["newer_than_days"]):
        return False
    return True


def _coerce_filter(raw: dict) -> dict:
    """Keep only known filter fields, dropping anything the model invented."""
    f: dict = {}
    src = str(raw.get("source") or "").lower()
    if src in KNOWN_SOURCES:
        f["source"] = src
    for key in ("sender_contains", "subject_contains"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            f[key] = v.strip()
    for key in ("older_than_days", "newer_than_days"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and v >= 0:
            f[key] = float(v)
    tag = str(raw.get("tag") or "").lower()
    if tag in VALID_TAGS:
        f["tag"] = tag
    urg = str(raw.get("urgency") or "").lower()
    if urg in VALID_URGENCY:
        f["urgency"] = urg
    if raw.get("watchlist") is True:
        f["watchlist"] = True
    f["unhandled_only"] = raw.get("unhandled_only", True) is not False
    return f


def _gather(source_filter: str | None) -> list[dict]:
    sources = [source_filter] if source_filter else list(KNOWN_SOURCES)
    out: list[dict] = []
    for s in sources:
        out.extend(cursor_store.list_cached(s, limit=2000))
    return out


@router.post("/plan")
async def plan(payload: dict) -> dict:
    """LLM-plan a command and dry-run it: returns the matched items, no changes."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        result = await runtime.call_json(
            PLAN_PROMPT.format(text=text), model=config.LLM_MODEL_CHEAP
        )
    except Exception as e:
        log.warning("command plan failed: %s", e)
        raise HTTPException(502, f"LLM planning failed: {e}")

    action = str(result.get("action") or "search").lower()
    if action not in VALID_ACTIONS:
        action = "search"
    f = _coerce_filter(result.get("filter") or {})
    summary = str(result.get("summary") or "").strip()[:200]

    items = _gather(f.get("source"))
    matches = [p for p in items if match_item(p, f)]
    matches.sort(key=lambda p: _age_days(p) or 0.0)  # oldest first
    preview = [{
        "source": p.get("source") or f.get("source") or "gmail",
        "msg_id": p.get("msg_id"),
        "subject": (p.get("subject") or p.get("summary") or "")[:90],
        "sender": p.get("sender_name") or p.get("sender_email") or p.get("channel_name") or "",
        "tag": p.get("tag"),
        "urgency": p.get("urgency"),
        "age_days": round(_age_days(p), 1) if _age_days(p) is not None else None,
    } for p in matches]
    return {"action": action, "filter": f, "summary": summary,
            "count": len(preview), "matches": preview}


@router.post("/execute")
async def execute(payload: dict) -> dict:
    """Execute a confirmed plan. Only 'done' mutates; 'search' is a no-op here.
    Gmail items are marked done in Gmail (read + archive + Haven/Done label)."""
    action = str(payload.get("action") or "").lower()
    items = payload.get("items") or []
    if action != "done":
        return {"done": 0, "action": action}

    by_source: dict[str, list[str]] = {}
    for it in items:
        src = str(it.get("source") or "").lower()
        mid = it.get("msg_id")
        if src in KNOWN_SOURCES and mid:
            by_source.setdefault(src, []).append(mid)

    # Gmail: one batchModify (read + archive + label). Hard-fails if Gmail errors.
    if by_source.get("gmail"):
        await gmail_actions.archive_ids(by_source["gmail"])

    done = 0
    now = time.time()
    for src, ids in by_source.items():
        cached = cursor_store.get_cached_payloads(src, ids)
        for mid in ids:
            item = cached.get(mid)
            if not item:
                continue
            item["handled_at"] = now
            cursor_store.put_cached(src, mid, item)
            await bus.publish(f"{src}_handled", {"msg_id": mid, "handled_at": now})
            done += 1
    return {"done": done, "action": action}
