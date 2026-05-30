"""Otter.ai fetcher — pulls action items assigned to Garth from his recent meetings.

Phase 2.2 design notes:
  - Read-only. Action items persist in Haven until the user marks-done / snoozes /
    captures to Linear / dismisses; the source has no "completed AR" state in the
    public API, so we don't auto-evict.
  - No LLM scoring. tag/urgency/action_required are deterministic — every Otter
    AR is by definition `tag=action, action_required=true, urgency=med`.
  - Bearer token auth: `Authorization: Bearer {OTTER_API_KEY}`.
  - 429 handling: respect Retry-After once.
  - One Otter AR -> one Haven item. msg_id = `otter:{conversation_id}:{md5(text)[:10]}`
    so the same AR survives polls even though Otter currently returns `id: null`
    on every action item.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import yaml

from haven import config

log = logging.getLogger(__name__)


def load_config() -> dict:
    p = config.AGENTS_CONFIG_DIR / "otter.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _ar_hash(text: str) -> str:
    return hashlib.md5((text or "").strip().lower().encode("utf-8")).hexdigest()[:10]


@dataclass
class OtterItem:
    """One action item from one Otter meeting."""
    conversation_id: str
    conversation_title: str
    conversation_url: str
    conversation_created_at: str       # ISO; we treat as the meeting date
    abstract_summary: str
    text: str                           # the AR itself
    assignee_name: str
    assignee_email: str
    status: str | None                  # Otter's own status field (often null)
    calendar_guests: list[dict] = field(default_factory=list)

    @property
    def msg_id(self) -> str:
        return f"otter:{self.conversation_id}:{_ar_hash(self.text)}"

    def summary(self) -> dict[str, Any]:
        # Truncate the AR for the subject line; keep full text in summary/suggested_action.
        short = self.text.strip()
        if len(short) > 110:
            short = short[:107] + "..."
        guest_emails = [g.get("email", "") for g in self.calendar_guests if g.get("email")]
        return {
            "source": "otter",
            "msg_id": self.msg_id,
            "conversation_id": self.conversation_id,
            "subject": short,
            "sender_name": self.conversation_title,           # meeting title shows in "from" slot
            "sender_email": self.assignee_email or "",
            "sender_company": "",
            "date": self.conversation_created_at,
            "snippet": self.text,
            "body_text": self.text,
            "deeplink": self.conversation_url,
            "labels": [],
            "has_attachment": False,
            "attachment_count": 0,
            # Otter-specific context
            "meeting_title": self.conversation_title,
            "meeting_summary": self.abstract_summary,
            "ar_text": self.text,
            "ar_status": self.status,
            "assignee_name": self.assignee_name,
            "assignee_email": self.assignee_email,
            "calendar_guest_emails": guest_emails,
            # Deterministic scoring (no LLM)
            "tag": "action",
            "urgency": "med",
            "action_required": True,
            "reply_needed": False,
            "reply_reason": "",
            "summary": self.text,
            "suggested_action": "Capture as Linear AR or mark done.",
            "suggested_reply": "",
        }


class OtterClient:
    """Thin httpx wrapper for Otter.ai Public API.

    Endpoints used:
      GET /v1/conversations           — user-scoped list (cursor pagination)
      GET /v1/conversations/{id}      — full conversation incl. relationships.action_items
      GET /v1/workspace               — owner email, workspace context (used by status endpoint)
    """

    def __init__(self) -> None:
        self.api_key = config.OTTER_API_KEY
        self.base = (config.OTTER_API_BASE or "https://api.otter.ai/v1").rstrip("/")
        self._owner_email: str | None = None
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict:
        if not self.api_key:
            raise RuntimeError("OTTER_API_KEY missing in .env")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _http(self) -> httpx.AsyncClient:
        """Lazily create + reuse one client across a poll's calls."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _call(self, path: str, params: dict | None = None, *, retried: bool = False) -> dict:
        url = f"{self.base}{path}"
        c = self._http()
        r = await c.get(url, headers=self._headers(), params=params or {})
        if r.status_code == 429 and not retried:
            wait = float(r.headers.get("Retry-After", "1"))
            log.warning("Otter %s 429 — sleeping %.1fs then retrying", path, wait)
            await asyncio.sleep(wait + 0.5)
            return await self._call(path, params, retried=True)
        if r.status_code != 200:
            raise RuntimeError(f"Otter {path} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    async def workspace(self) -> dict:
        """Returns workspace data (id, name, owner.email). Used to verify auth + scope owner email."""
        j = await self._call("/workspace")
        return j.get("data") or {}

    async def owner_email(self) -> str:
        if self._owner_email is not None:
            return self._owner_email
        ws = await self.workspace()
        email = ((ws.get("owner") or {}).get("email") or "").lower()
        self._owner_email = email
        return email

    async def list_conversations(
        self,
        *,
        max_pages: int = 10,
        page_size: int = 50,
        stop_before: datetime | None = None,
    ) -> list[dict]:
        """Pull conversations via cursor pagination. Stops early once a page contains
        a conversation older than `stop_before` (saves API calls — list is reverse-chronological)."""
        all_convs: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size}
            if cursor:
                params["cursor"] = cursor
            j = await self._call("/conversations", params)
            page = j.get("data") or []
            all_convs.extend(page)

            # Early-stop: if any conversation in this page is older than stop_before,
            # the next page would be older still — break.
            if stop_before is not None and page:
                oldest_in_page = _parse_iso((page[-1] or {}).get("created_at"))
                if oldest_in_page is not None and oldest_in_page < stop_before:
                    break

            meta = j.get("meta") or {}
            if not meta.get("has_more"):
                break
            cursor = meta.get("next_cursor")
            if not cursor:
                break
        return all_convs

    async def get_conversation(self, conversation_id: str) -> dict:
        """Full conversation incl. relationships.action_items / insights / outline / transcript."""
        j = await self._call(f"/conversations/{conversation_id}")
        return j.get("data") or {}


class OtterFetcher:
    def __init__(self) -> None:
        self.client = OtterClient()
        self.cfg = load_config()
        scoring = self.cfg.get("scoring") or {}
        self.lookback_days = int(scoring.get("lookback_days", 7))
        identity = self.cfg.get("identity") or {}
        # Override owner_email via yaml if set; otherwise resolve from API.
        self._configured_email: str | None = (identity.get("email") or "").lower() or None
        self._max_concurrent_details = int((self.cfg.get("scoring") or {}).get("max_concurrent_fetches", 5))

    async def fetch_all(self) -> list[OtterItem]:
        """Public entry: runs the fetch and always closes the shared HTTP client."""
        try:
            return await self._fetch_all()
        finally:
            await self.client.aclose()

    async def _fetch_all(self) -> list[OtterItem]:
        """List recent conversations, fetch full body for each that has finished
        action-item processing, filter ARs to those assigned to Garth."""
        owner_email = self._configured_email or await self.client.owner_email()
        if not owner_email:
            raise RuntimeError("Could not resolve Otter owner email (yaml identity.email and /workspace both empty)")
        log.info("Otter: fetching last %d days for %s", self.lookback_days, owner_email)

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        convs = await self.client.list_conversations(stop_before=cutoff, page_size=50, max_pages=10)

        # Filter: in window + has finished action-item processing
        in_window: list[dict] = []
        for c in convs:
            created = _parse_iso(c.get("created_at"))
            if not created or created < cutoff:
                continue
            ps = (c.get("process_status") or {})
            if ps.get("action_item") != "finished":
                continue
            in_window.append(c)
        log.info("Otter: %d conversations in window (out of %d listed)", len(in_window), len(convs))

        # Fetch full bodies in parallel (capped) so we get action items
        sem = asyncio.Semaphore(self._max_concurrent_details)

        async def _detail(cid: str) -> dict | None:
            async with sem:
                try:
                    return await self.client.get_conversation(cid)
                except Exception as e:
                    log.warning("Otter detail %s failed: %s", cid, e)
                    return None

        details = await asyncio.gather(*[_detail(c["id"]) for c in in_window if c.get("id")])

        items: list[OtterItem] = []
        skipped_other_assignee = 0
        skipped_unassigned = 0
        for d in details:
            if not d:
                continue
            cid = d.get("id") or ""
            ctitle = d.get("title") or ""
            curl = d.get("url") or f"https://otter.ai/u/{cid}"
            ccreated = d.get("created_at") or ""
            cabstract = d.get("abstract_summary") or ""
            cguests = d.get("calendar_guests") or []
            rels = d.get("relationships") or {}
            ars = rels.get("action_items") or []
            for ar in ars:
                text = (ar.get("text") or "").strip()
                if not text:
                    continue
                assignee = ar.get("assignee") or {}
                ae = (assignee.get("email") or "").lower().strip()
                if not ae:
                    # Phase 2.2 v1: skip unattributed. (LLM tagging is a possible follow-up.)
                    skipped_unassigned += 1
                    continue
                if ae != owner_email:
                    skipped_other_assignee += 1
                    continue
                items.append(OtterItem(
                    conversation_id=cid,
                    conversation_title=ctitle,
                    conversation_url=curl,
                    conversation_created_at=ccreated,
                    abstract_summary=cabstract,
                    text=text,
                    assignee_name=assignee.get("name") or "",
                    assignee_email=ae,
                    status=ar.get("status"),
                    calendar_guests=cguests,
                ))

        log.info(
            "Otter: %d ARs assigned to Garth (skipped %d to others, %d unassigned)",
            len(items), skipped_other_assignee, skipped_unassigned,
        )
        return items
