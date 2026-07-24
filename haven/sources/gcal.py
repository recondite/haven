"""Google Calendar — find a person's recurring 1:1 with Garth.

Read-only. Used by the person page (SIM-203) to surface, for direct reports,
the next 1:1 meeting time and the Google Doc linked to that event.

Identification (per the chosen rule): a calendar event is "the 1:1" when its
title references the person AND looks like a 1:1 — e.g. "Garth / Priya",
"Priya / Garth", "Priya 1:1", "Priya <> Garth". The soonest upcoming match wins.
The 1:1 doc = a Google-Doc attachment on the event, else the first
docs.google.com link in the description.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("haven")

_DOC_LINK_RE = re.compile(r"https://docs\.google\.com/\S+")
_ONE_ON_ONE_MARKERS = ("1:1", "1-1", "one-on-one", "one on one", "o3", "<>", "sync", "check-in", "checkin")
_DOC_MIME = "application/vnd.google-apps.document"


def _first_names(full_name: str) -> list[str]:
    return [p for p in re.split(r"\s+", (full_name or "").strip()) if p]


def _event_start(ev: dict) -> str:
    s = ev.get("start") or {}
    return s.get("dateTime") or s.get("date") or ""


def _looks_like_1on1(summary: str, person_name: str, self_name: str) -> bool:
    """Title references the person and reads like a 1:1 (marker, or paired with
    Garth via a separator)."""
    s = (summary or "").lower()
    parts = [p.lower() for p in _first_names(person_name)]
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    has_person = first in s or (len(last) > 2 and last in s)
    if not has_person:
        return False
    if any(m in s for m in _ONE_ON_ONE_MARKERS):
        return True
    # "Garth / Priya" style: both names present with a separator.
    self_first = (_first_names(self_name)[0] if _first_names(self_name) else "").lower()
    if self_first and self_first in s and any(sep in s for sep in ("/", "|", "&", " and ", ":")):
        return True
    return False


def _extract_doc(ev: dict) -> str:
    for a in ev.get("attachments") or []:
        if a.get("mimeType") == _DOC_MIME and a.get("fileUrl"):
            return a["fileUrl"]
    # Fallback: first Google-Docs link in the description.
    m = _DOC_LINK_RE.search(ev.get("description") or "")
    return m.group(0) if m else ""


def pick_one_on_one(events: list[dict], person_name: str, self_name: str = "Garth") -> dict | None:
    """Pure selector. `events` should be upcoming, start-sorted. Returns
    {summary, next_time, doc_url} for the soonest 1:1 match, or None."""
    for ev in events:
        if _looks_like_1on1(ev.get("summary") or "", person_name, self_name):
            return {
                "summary": ev.get("summary") or "",
                "next_time": _event_start(ev),
                "doc_url": _extract_doc(ev),
            }
    return None


async def next_one_on_one(gmail_auth, person_name: str, self_name: str = "Garth") -> dict | None:
    """Fetch upcoming events matching the person's name and pick the 1:1.
    Returns None (silently) when calendar scope isn't granted or nothing matches."""
    import asyncio
    from datetime import datetime, timezone

    service = await gmail_auth.get_calendar_service()
    if service is None:
        return None
    first = _first_names(person_name)
    if not first:
        return None
    now = datetime.now(timezone.utc).isoformat()

    def _list() -> list[dict]:
        resp = service.events().list(
            calendarId="primary", timeMin=now, q=first[0],
            singleEvents=True, orderBy="startTime", maxResults=15,
        ).execute()
        return resp.get("items") or []

    try:
        events = await asyncio.to_thread(_list)
    except Exception as e:  # noqa: BLE001
        log.debug("gcal 1:1 lookup failed for %s: %s", person_name, e)
        return None
    return pick_one_on_one(events, person_name, self_name)
