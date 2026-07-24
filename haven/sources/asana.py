"""Asana fetcher — pulls Garth's incomplete assigned tasks.

Phase 1 design notes (mirrors jira.py / freshservice.py):
  - Read-only. Tasks persist in Haven only while incomplete + assigned; the
    poll handler in routers/asana.py evicts tasks that leave the result set.
  - No LLM scoring. tag/urgency/action_required are derived deterministically
    from the due date inside `summary()` (Asana has no built-in priority).
  - Auth: Bearer Personal Access Token.
  - `completed_since=now` returns only incomplete tasks. Offset pagination.
  - 429 handling: respect Retry-After once.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import yaml

from haven import config

log = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"
_OPT_FIELDS = (
    "name,notes,due_on,due_at,completed,permalink_url,modified_at,created_at,"
    "assignee.gid,assignee.name,projects.name"
)


def load_config() -> dict:
    p = config.AGENTS_CONFIG_DIR / "asana.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _due_dt(due_at: str | None, due_on: str | None) -> datetime | None:
    """due_at is a full timestamp; due_on is a date (end-of-day UTC)."""
    if due_at:
        return parse_iso(due_at)
    if due_on:
        return parse_iso(f"{due_on}T23:59:59+00:00")
    return None


@dataclass
class AsanaItem:
    gid: str
    name: str
    notes: str
    due_at: str | None
    due_on: str | None
    permalink_url: str
    assignee_gid: str
    assignee_name: str
    projects: list[str]
    created_at: str
    modified_at: str
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def msg_id(self) -> str:
        return f"asana:{self.gid}"

    def summary(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        due = _due_dt(self.due_at, self.due_on)
        sla_breached = bool(due and due < now)
        sla_at_risk = bool(due and not sla_breached and due < now + timedelta(hours=48))
        urgency = self._derive_urgency(sla_breached, sla_at_risk, bool(due))

        snippet = (self.notes or "").strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        summary_text = (self.notes or self.name or "").strip().replace("\n", " ")
        if len(summary_text) > 120:
            summary_text = summary_text[:117] + "..."
        project = self.projects[0] if self.projects else ""

        return {
            "source": "asana",
            "msg_id": self.msg_id,
            "subject": f"[{project}] {self.name}" if project else self.name,
            "sender_name": self.assignee_name or "Unassigned",
            "sender_email": "",                  # Asana task payloads carry no email
            "sender_company": "",
            "sender_domain": "",
            "date": self.modified_at or self.created_at,
            "deeplink": self.permalink_url,
            "snippet": snippet,
            "body_text": self.notes or "",
            "labels": list(self.projects or []),
            # Asana-specific fields
            "task_gid": self.gid,
            "assignee_gid": self.assignee_gid,
            "assignee_name": self.assignee_name,
            "project_name": project,
            "due_by": self.due_on or self.due_at,
            "sla_breached": sla_breached,
            "sla_at_risk": sla_at_risk,
            # Deterministic score block (no LLM)
            "tag": "action",
            "urgency": urgency,
            "action_required": True,
            "reply_needed": False,
            "reply_reason": "",
            "summary": summary_text,
            "suggested_action": "Complete this task" if due else "Review",
            "suggested_reply": "",
            "has_attachment": False,
            "attachment_count": 0,
        }

    @staticmethod
    def _derive_urgency(sla_breached: bool, sla_at_risk: bool, has_due: bool) -> str:
        if sla_breached:
            return "urgent"
        if sla_at_risk:
            return "high"
        if has_due:
            return "med"
        return "low"


class AsanaClient:
    def __init__(self) -> None:
        self.token = config.ASANA_TOKEN or ""
        self.workspace = config.ASANA_WORKSPACE or ""
        self._me_gid: str | None = None
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, path: str, *, params: dict | None = None,
                    retried: bool = False) -> Any:
        if not self.token:
            raise RuntimeError("Asana not configured (ASANA_TOKEN)")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        c = self._http()
        r = await c.request(method, f"{_BASE}{path}", headers=headers, params=params or {})
        if r.status_code == 429 and not retried:
            wait = float(r.headers.get("Retry-After", "1"))
            log.warning("Asana %s %s 429 — sleeping %.1fs then retrying", method, path, wait)
            await asyncio.sleep(wait + 0.5)
            return await self._call(method, path, params=params, retried=True)
        if r.status_code >= 400:
            raise RuntimeError(f"Asana {method} {path} HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    async def me(self) -> dict:
        """Return {gid, workspace_gid}. Caches accountId; resolves workspace from
        config or the first workspace on /users/me."""
        j = await self._call("GET", "/users/me", params={"opt_fields": "workspaces.name"})
        data = (j or {}).get("data") or {}
        self._me_gid = str(data.get("gid") or "")
        if not self.workspace:
            wss = data.get("workspaces") or []
            self.workspace = str(wss[0]["gid"]) if wss else ""
        return {"gid": self._me_gid, "workspace_gid": self.workspace}

    async def tasks_assigned_to_me(self, *, page_size: int, max_pages: int) -> list[dict]:
        if not self.workspace:
            await self.me()
        tasks: list[dict] = []
        offset: str | None = None
        for _ in range(max_pages):
            params = {
                "assignee": "me",
                "workspace": self.workspace,
                "completed_since": "now",     # incomplete tasks only
                "opt_fields": _OPT_FIELDS,
                "limit": str(page_size),
            }
            if offset:
                params["offset"] = offset
            j = await self._call("GET", "/tasks", params=params) or {}
            tasks.extend(j.get("data") or [])
            offset = ((j.get("next_page") or {}) or {}).get("offset")
            if not offset:
                break
        return tasks


class AsanaFetcher:
    def __init__(self, client: AsanaClient | None = None) -> None:
        self.client = client or AsanaClient()
        self.cfg = load_config()
        nk = self.cfg.get("never_keep") or {}
        self.never_keep_projects: set[str] = {
            str(p).lower() for p in (nk.get("project_names") or [])
        }
        self.page_size = int(self.cfg.get("page_size", 100))
        self.max_pages = int(self.cfg.get("max_pages", 5))

    async def fetch_all(self) -> list[AsanaItem]:
        try:
            return await self._fetch_all()
        finally:
            await self.client.aclose()

    async def _fetch_all(self) -> list[AsanaItem]:
        raw = await self.client.tasks_assigned_to_me(
            page_size=self.page_size, max_pages=self.max_pages
        )
        items: list[AsanaItem] = []
        for t in raw:
            if t.get("completed"):
                continue                      # belt-and-braces
            projects = [str((p or {}).get("name") or "") for p in (t.get("projects") or [])]
            if any(p.lower() in self.never_keep_projects for p in projects if p):
                continue
            assignee = t.get("assignee") or {}
            items.append(AsanaItem(
                gid=str(t["gid"]),
                name=str(t.get("name") or "(no name)"),
                notes=str(t.get("notes") or ""),
                due_at=t.get("due_at"),
                due_on=t.get("due_on"),
                permalink_url=str(t.get("permalink_url") or ""),
                assignee_gid=str(assignee.get("gid") or ""),
                assignee_name=str(assignee.get("name") or ""),
                projects=[p for p in projects if p],
                created_at=str(t.get("created_at") or ""),
                modified_at=str(t.get("modified_at") or t.get("created_at") or ""),
            ))
        items.sort(key=lambda i: i.modified_at, reverse=True)
        return items
