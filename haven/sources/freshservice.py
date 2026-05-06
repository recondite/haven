"""Freshservice fetcher — pulls open IT tickets where Garth is the agent or the
ticket is unassigned.

Phase 2.1 design notes:
  - Read-only. Tickets persist in Haven only as long as they are Open/Pending in
    Freshservice; the poll handler in main.py drops cached tickets that fall
    out of the result set.
  - No LLM scoring. tag/urgency/action_required are derived deterministically
    from priority + SLA fields inside `summary()`.
  - HTTP Basic auth: API key as username, "X" (anything) as password.
  - 429 handling: respect Retry-After once.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import yaml

from haven import config

log = logging.getLogger(__name__)

STATUS_LABEL = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
PRIORITY_LABEL = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


def load_config() -> dict:
    p = config.AGENTS_CONFIG_DIR / "freshservice.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Freshservice timestamps are ISO 8601 with trailing Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


@dataclass
class FreshserviceItem:
    ticket_id: int
    subject: str
    description_text: str
    status: int
    priority: int
    responder_id: int | None
    requester_id: int
    requester_name: str
    requester_email: str
    created_at: str
    updated_at: str
    due_by: str | None
    fr_due_by: str | None
    type_: str
    category: str | None
    tags: list[str]
    domain: str
    agent_id: int | None             # Garth's agent_id, for is_assigned_to_me
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def msg_id(self) -> str:
        return f"fs:{self.ticket_id}"

    def summary(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        due = _parse_iso(self.due_by)
        sla_breached = bool(due and due < now)
        sla_at_risk = bool(due and not sla_breached and due < now + timedelta(hours=24))
        is_unassigned = self.responder_id is None
        is_assigned_to_me = (
            self.agent_id is not None and self.responder_id == self.agent_id
        )

        # Urgency = priority only (per Phase 2.1 product decision). SLA flags
        # are still surfaced as data, but don't bump urgency — Freshservice's
        # default SLAs flag stale tickets as breached, producing too much noise.
        urgency = self._derive_urgency()
        suggested = self._suggested_action(is_unassigned, is_assigned_to_me)

        snippet = (self.description_text or "").strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."

        summary_text = (self.description_text or self.subject or "").strip().replace("\n", " ")
        if len(summary_text) > 120:
            summary_text = summary_text[:117] + "..."

        return {
            "source": "freshservice",
            "msg_id": self.msg_id,
            "subject": f"[#{self.ticket_id}] {self.subject}",
            "sender_name": self.requester_name or self.requester_email or "Unknown requester",
            "sender_email": self.requester_email,
            "sender_company": "",
            "sender_domain": "",
            "date": self.updated_at or self.created_at,
            "deeplink": f"https://{self.domain}/a/tickets/{self.ticket_id}",
            "snippet": snippet,
            "body_text": self.description_text or "",
            "labels": list(self.tags or []),
            # Freshservice-specific fields
            "ticket_id": self.ticket_id,
            "status_code": self.status,
            "status_label": STATUS_LABEL.get(self.status, str(self.status)),
            "priority_code": self.priority,
            "priority_label": PRIORITY_LABEL.get(self.priority, str(self.priority)),
            "ticket_type": self.type_,
            "category": self.category,
            "is_unassigned": is_unassigned,
            "is_assigned_to_me": is_assigned_to_me,
            "due_by": self.due_by,
            "fr_due_by": self.fr_due_by,
            "sla_breached": sla_breached,
            "sla_at_risk": sla_at_risk,
            # Deterministic score block (no LLM)
            "tag": "action",
            "urgency": urgency,
            "action_required": True,
            "reply_needed": False,
            "reply_reason": "",
            "summary": summary_text,
            "suggested_action": suggested,
            "suggested_reply": "",
            "has_attachment": False,
            "attachment_count": 0,
        }

    def _derive_urgency(self) -> str:
        # Direct priority → urgency map. Priority 4 (Urgent) → urgent, etc.
        return {4: "urgent", 3: "high", 2: "med", 1: "low"}.get(self.priority, "low")

    @staticmethod
    def _suggested_action(unassigned: bool, assigned_to_me: bool) -> str:
        if unassigned:
            return "Triage and assign"
        if assigned_to_me:
            return "Resolve this ticket"
        return "Review"


class FreshserviceClient:
    def __init__(self) -> None:
        self.domain = config.FRESHSERVICE_DOMAIN or ""
        self.api_key = config.FRESHSERVICE_API_KEY or ""
        self._agent_id: int | None = None

    @property
    def base(self) -> str:
        return f"https://{self.domain}/api/v2"

    @property
    def _auth_header(self) -> str:
        # HTTP Basic: api_key:X
        token = base64.b64encode(f"{self.api_key}:X".encode()).decode()
        return f"Basic {token}"

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retried: bool = False,
    ) -> Any:
        if not self.domain or not self.api_key:
            raise RuntimeError("Freshservice not configured (FRESHSERVICE_DOMAIN/FRESHSERVICE_API_KEY)")
        headers = {"Authorization": self._auth_header, "Content-Type": "application/json"}
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, url, headers=headers, params=params or {}, json=json)

        if r.status_code == 429 and not retried:
            wait = float(r.headers.get("Retry-After", "1"))
            log.warning("Freshservice %s %s 429 — sleeping %.1fs then retrying", method, path, wait)
            await asyncio.sleep(wait + 0.5)
            return await self._call(method, path, params=params, json=json, retried=True)

        if r.status_code >= 400:
            raise RuntimeError(f"Freshservice {method} {path} HTTP {r.status_code}: {r.text[:300]}")
        if not r.content:
            return None
        return r.json()

    async def self_agent_id(self, email: str | None = None) -> int:
        """Resolve and cache Garth's agent_id. Freshservice exposes /agents/me on
        accounts where the API key belongs to an agent; otherwise we fall back
        to a search by email."""
        if self._agent_id is not None:
            return self._agent_id
        try:
            j = await self._call("GET", "/agents/me")
            agent = (j or {}).get("agent") or {}
            aid = agent.get("id")
            if aid:
                self._agent_id = int(aid)
                return self._agent_id
        except Exception as e:
            log.warning("Freshservice /agents/me unavailable, falling back to search: %s", e)

        if email:
            try:
                j = await self._call("GET", "/agents", params={"email": email})
                agents = (j or {}).get("agents") or []
                if agents:
                    self._agent_id = int(agents[0]["id"])
                    return self._agent_id
            except Exception as e:
                log.warning("Freshservice agent search by email failed: %s", e)

        raise RuntimeError("Could not resolve Freshservice agent_id for this API key")

    async def list_tickets(self, *, query: str, page: int = 1, per_page: int = 100) -> dict:
        """Filter API. Note: Freshservice filter endpoint is `/tickets/filter`
        with `query` param wrapped in double-quotes."""
        params = {"query": f'"{query}"', "page": str(page), "per_page": str(per_page)}
        return await self._call("GET", "/tickets/filter", params=params) or {}

    async def get_ticket(self, ticket_id: int, include: str | None = None) -> dict:
        params: dict[str, str] = {}
        if include:
            params["include"] = include
        j = await self._call("GET", f"/tickets/{ticket_id}", params=params) or {}
        return j.get("ticket") or {}

    async def get_requester(self, requester_id: int) -> dict:
        try:
            j = await self._call("GET", f"/requesters/{requester_id}") or {}
            return j.get("requester") or {}
        except Exception as e:
            log.debug("Freshservice requester lookup %s failed: %s", requester_id, e)
            return {}


class FreshserviceFetcher:
    def __init__(self, client: FreshserviceClient | None = None) -> None:
        self.client = client or FreshserviceClient()
        self.cfg = load_config()
        self.email: str = (self.cfg.get("identity") or {}).get("email") or ""
        self.statuses: list[int] = list(self.cfg.get("include_statuses") or [2, 3])
        scopes = self.cfg.get("include_scopes") or {}
        self.scope_assigned = bool(scopes.get("assigned_to_me", True))
        self.scope_unassigned = bool(scopes.get("unassigned", True))
        self.unassigned_max_age_days = int(self.cfg.get("unassigned_max_age_days", 122))  # ~4 months
        self.per_page = int(self.cfg.get("per_page", 100))
        self.max_pages = int(self.cfg.get("max_pages", 5))
        nk = self.cfg.get("never_keep") or []
        self.never_keep_tags: set[str] = {
            str(d.get("tag")).lower() for d in nk if isinstance(d, dict) and d.get("tag")
        }
        self.never_keep_statuses: set[int] = {
            int(d.get("status")) for d in nk if isinstance(d, dict) and d.get("status") is not None
        }

    def _build_query(self, agent_id: int) -> str:
        # Freshservice FQL: list of statuses → "(status:2 OR status:3)"
        status_clause = " OR ".join(f"status:{s}" for s in self.statuses) or "status:2"
        agent_terms: list[str] = []
        if self.scope_assigned:
            agent_terms.append(f"agent_id:{agent_id}")
        if self.scope_unassigned:
            # Age-cap unassigned tickets so stale backlog doesn't drown the dashboard.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.unassigned_max_age_days)).date().isoformat()
            agent_terms.append(f"(agent_id:null AND created_at:>'{cutoff}')")
        agent_clause = " OR ".join(agent_terms) or f"agent_id:{agent_id}"
        return f"({agent_clause}) AND ({status_clause})"

    async def fetch_all(self) -> list[FreshserviceItem]:
        agent_id = await self.client.self_agent_id(self.email or None)
        query = self._build_query(agent_id)
        log.info("Freshservice query: %s", query)

        all_tickets: list[dict] = []
        for page in range(1, self.max_pages + 1):
            j = await self.client.list_tickets(query=query, page=page, per_page=self.per_page)
            page_tickets = j.get("tickets") or []
            all_tickets.extend(page_tickets)
            if len(page_tickets) < self.per_page:
                break

        # Resolve requester names lazily (only ones we haven't seen).
        requester_cache: dict[int, dict] = {}

        async def _requester(rid: int) -> dict:
            if rid in requester_cache:
                return requester_cache[rid]
            r = await self.client.get_requester(rid)
            requester_cache[rid] = r
            return r

        items: list[FreshserviceItem] = []
        for t in all_tickets:
            tags = [str(x) for x in (t.get("tags") or [])]
            # Apply never_keep filters
            if any(tag.lower() in self.never_keep_tags for tag in tags):
                continue
            status = int(t.get("status") or 0)
            if status in self.never_keep_statuses:
                continue
            if status not in self.statuses:
                # Belt-and-braces: filter API should already enforce this
                continue

            requester_id = int(t.get("requester_id") or 0)
            requester = await _requester(requester_id) if requester_id else {}
            req_name = " ".join(
                p for p in [requester.get("first_name"), requester.get("last_name")] if p
            ).strip()
            req_email = requester.get("primary_email") or requester.get("email") or ""

            description_text = (
                t.get("description_text")
                or _strip_html(t.get("description"))
                or ""
            )

            items.append(FreshserviceItem(
                ticket_id=int(t["id"]),
                subject=str(t.get("subject") or "(no subject)"),
                description_text=description_text,
                status=status,
                priority=int(t.get("priority") or 1),
                responder_id=(int(t["responder_id"]) if t.get("responder_id") is not None else None),
                requester_id=requester_id,
                requester_name=req_name,
                requester_email=req_email,
                created_at=str(t.get("created_at") or ""),
                updated_at=str(t.get("updated_at") or t.get("created_at") or ""),
                due_by=t.get("due_by"),
                fr_due_by=t.get("fr_due_by"),
                type_=str(t.get("type") or ""),
                category=t.get("category"),
                tags=tags,
                domain=self.client.domain,
                agent_id=agent_id,
                extras={"raw_status": t.get("status"), "source_obj": "ticket"},
            ))

        # Most-recently-updated first
        items.sort(key=lambda i: i.updated_at, reverse=True)
        return items


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    # Minimal tag stripper — Freshservice rich-text is simple HTML.
    import re
    no_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", no_tags).strip()
