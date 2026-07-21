"""Jira fetcher — pulls issues needing Garth's attention via configurable JQL scopes.

Phase 1 design notes (mirrors freshservice.py):
  - Read-only. Issues persist in Haven only while they match an enabled scope's
    JQL; the poll handler in routers/jira.py evicts cached issues that fall out
    of the result set.
  - No LLM scoring. tag/urgency/action_required are derived deterministically
    from priority + due date inside `summary()`.
  - HTTP Basic auth: email:api_token.
  - Uses POST /rest/api/3/search/jql with nextPageToken pagination — the legacy
    /rest/api/3/search endpoint was removed from Jira Cloud (410) in 2025. It
    returns no fields unless explicitly requested.
  - A scope whose JQL errors is logged and skipped — one bad query must not
    kill the whole poll.
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

_FIELDS = [
    "summary", "status", "priority", "issuetype", "project",
    "assignee", "reporter", "created", "updated", "duedate",
    "description", "labels",
]

_URGENCY_BY_PRIORITY = {"highest": "urgent", "high": "high", "medium": "med"}
_URGENCY_BUMP = {"low": "med", "med": "high", "high": "urgent", "urgent": "urgent"}


def load_config() -> dict:
    p = config.AGENTS_CONFIG_DIR / "jira.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Jira timestamps look like "2026-07-21T14:30:00.000-0700"; Python
        # 3.11+ fromisoformat accepts both -0700 and -07:00 offsets and Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _adf_text(node: Any) -> str:
    """Extract plain text from an Atlassian Document Format tree (API v3
    descriptions). Also tolerates plain strings (API v2-style)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(t for t in (_adf_text(n) for n in node) if t)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text") or ""
        return _adf_text(node.get("content"))
    return ""


@dataclass
class JiraItem:
    key: str
    summary_text: str
    description_text: str
    status_name: str
    status_category: str            # new | indeterminate | done
    priority_name: str
    issue_type: str
    project_key: str
    assignee_account_id: str
    assignee_name: str
    reporter_account_id: str
    reporter_name: str
    reporter_email: str             # often "" — Jira GDPR settings redact emails
    created: str
    updated: str
    duedate: str | None             # date only, e.g. "2026-07-25"
    labels: list[str]
    base_url: str
    my_account_id: str
    matched_scopes: list[str] = field(default_factory=list)

    @property
    def msg_id(self) -> str:
        return f"jira:{self.key}"

    def summary(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        due = parse_iso(f"{self.duedate}T23:59:59+00:00") if self.duedate else None
        sla_breached = bool(due and due < now)
        sla_at_risk = bool(due and not sla_breached and due < now + timedelta(hours=48))
        urgency = self._derive_urgency(sla_breached, sla_at_risk)

        snippet = (self.description_text or "").strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        summary_text = (self.description_text or self.summary_text or "").strip().replace("\n", " ")
        if len(summary_text) > 120:
            summary_text = summary_text[:117] + "..."

        return {
            "source": "jira",
            "msg_id": self.msg_id,
            "subject": f"[{self.key}] {self.summary_text}",
            "sender_name": self.reporter_name or self.reporter_email or "Unknown reporter",
            "sender_email": self.reporter_email,
            "sender_company": "",
            "sender_domain": "",
            "date": self.updated or self.created,
            "deeplink": f"{self.base_url}/browse/{self.key}",
            "snippet": snippet,
            "body_text": self.description_text or "",
            "labels": list(self.labels or []),
            # Jira-specific fields (status_label/priority_label/due_by/sla_*
            # reuse the generic detail-pane rendering built for Freshservice)
            "issue_key": self.key,
            "status_label": self.status_name,
            "status_category": self.status_category,
            "priority_label": self.priority_name,
            "issue_type": self.issue_type,
            "project_key": self.project_key,
            "assignee_account_id": self.assignee_account_id,
            "assignee_name": self.assignee_name,
            "reporter_account_id": self.reporter_account_id,
            "reporter_name": self.reporter_name,
            "is_assigned_to_me": bool(
                self.my_account_id and self.assignee_account_id == self.my_account_id
            ),
            "matched_scopes": list(self.matched_scopes),
            "due_by": self.duedate,
            "sla_breached": sla_breached,
            "sla_at_risk": sla_at_risk,
            # Deterministic score block (no LLM)
            "tag": "approval" if "pending_approvals" in self.matched_scopes else "action",
            "urgency": urgency,
            "action_required": True,
            "reply_needed": False,
            "reply_reason": "",
            "summary": summary_text,
            "suggested_action": self._suggested_action(),
            "suggested_reply": "",
            "has_attachment": False,
            "attachment_count": 0,
        }

    def _derive_urgency(self, sla_breached: bool, sla_at_risk: bool) -> str:
        urgency = _URGENCY_BY_PRIORITY.get(self.priority_name.lower(), "low")
        if sla_breached:
            return "urgent"
        if sla_at_risk:
            return _URGENCY_BUMP[urgency]
        return urgency

    def _suggested_action(self) -> str:
        if "pending_approvals" in self.matched_scopes:
            return "Approve or reject"
        if "assigned" in self.matched_scopes:
            return "Resolve this issue"
        if "watched_stale" in self.matched_scopes:
            return "Nudge — watched issue is stale"
        return "Review"


class JiraClient:
    def __init__(self) -> None:
        self.base_url = (config.JIRA_BASE_URL or "").rstrip("/")
        self.email = config.JIRA_EMAIL or ""
        self.api_token = config.JIRA_API_TOKEN or ""
        self._account_id: str | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def _auth_header(self) -> str:
        # HTTP Basic: email:api_token
        token = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        return f"Basic {token}"

    def _http(self) -> httpx.AsyncClient:
        """Lazily create + reuse one client across a poll's calls."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retried: bool = False,
    ) -> Any:
        if not self.base_url or not self.email or not self.api_token:
            raise RuntimeError("Jira not configured (JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN)")
        headers = {"Authorization": self._auth_header, "Content-Type": "application/json"}
        url = f"{self.base_url}{path}"
        c = self._http()
        r = await c.request(method, url, headers=headers, params=params or {}, json=json)

        if r.status_code == 429 and not retried:
            wait = float(r.headers.get("Retry-After", "1"))
            log.warning("Jira %s %s 429 — sleeping %.1fs then retrying", method, path, wait)
            await asyncio.sleep(wait + 0.5)
            return await self._call(method, path, params=params, json=json, retried=True)

        if r.status_code >= 400:
            raise RuntimeError(f"Jira {method} {path} HTTP {r.status_code}: {r.text[:300]}")
        if not r.content:
            return None
        return r.json()

    async def myself(self) -> str:
        """Resolve and cache Garth's accountId via /rest/api/3/myself."""
        if self._account_id is not None:
            return self._account_id
        j = await self._call("GET", "/rest/api/3/myself") or {}
        aid = j.get("accountId")
        if not aid:
            raise RuntimeError("Could not resolve Jira accountId for this API token")
        self._account_id = str(aid)
        return self._account_id

    async def search_jql(
        self,
        jql: str,
        *,
        max_results: int = 100,
        next_page_token: str | None = None,
    ) -> dict:
        """Enhanced-search endpoint (token pagination). Returns
        {"issues": [...], "nextPageToken": "..."} — token absent on last page."""
        body: dict[str, Any] = {"jql": jql, "fields": _FIELDS, "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        return await self._call("POST", "/rest/api/3/search/jql", json=body) or {}


class JiraFetcher:
    def __init__(self, client: JiraClient | None = None) -> None:
        self.client = client or JiraClient()
        self.cfg = load_config()
        self.scopes: list[dict] = [
            s for s in (self.cfg.get("scopes") or [])
            if isinstance(s, dict) and s.get("enabled") and s.get("jql")
        ]
        nk = self.cfg.get("never_keep") or {}
        self.never_keep_types: set[str] = {
            str(t).lower() for t in (nk.get("issue_types") or [])
        }
        self.never_keep_statuses: set[str] = {
            str(s).lower() for s in (nk.get("statuses") or [])
        }
        self.max_results = int(self.cfg.get("max_results", 100))
        self.max_pages = int(self.cfg.get("max_pages", 5))

    async def fetch_all(self) -> list[JiraItem]:
        """Public entry: runs the fetch and always closes the shared HTTP client."""
        try:
            return await self._fetch_all()
        finally:
            await self.client.aclose()

    async def _fetch_all(self) -> list[JiraItem]:
        my_account_id = await self.client.myself()

        by_key: dict[str, JiraItem] = {}
        for scope in self.scopes:
            name = str(scope.get("name") or "unnamed")
            try:
                issues = await self._search_scope(str(scope["jql"]))
            except Exception as e:
                # One bad JQL (e.g. JSM approval= on a non-JSM site) must not
                # kill the poll — log and move on.
                log.warning("Jira scope %r failed, skipping: %s", name, e)
                continue
            for raw in issues:
                key = str(raw.get("key") or "")
                if not key:
                    continue
                if key in by_key:
                    if name not in by_key[key].matched_scopes:
                        by_key[key].matched_scopes.append(name)
                    continue
                item = self._build_item(raw, my_account_id, name)
                if item is not None:
                    by_key[key] = item

        items = [
            i for i in by_key.values()
            if i.issue_type.lower() not in self.never_keep_types
            and i.status_name.lower() not in self.never_keep_statuses
        ]
        # Most-recently-updated first
        items.sort(key=lambda i: i.updated, reverse=True)
        return items

    async def _search_scope(self, jql: str) -> list[dict]:
        issues: list[dict] = []
        token: str | None = None
        for _ in range(self.max_pages):
            j = await self.client.search_jql(
                jql, max_results=self.max_results, next_page_token=token
            )
            issues.extend(j.get("issues") or [])
            token = j.get("nextPageToken")
            if not token:
                break
        return issues

    def _build_item(self, raw: dict, my_account_id: str, scope_name: str) -> JiraItem | None:
        f = raw.get("fields") or {}
        status = f.get("status") or {}
        assignee = f.get("assignee") or {}
        reporter = f.get("reporter") or {}
        return JiraItem(
            key=str(raw["key"]),
            summary_text=str(f.get("summary") or "(no summary)"),
            description_text=_adf_text(f.get("description")),
            status_name=str(status.get("name") or ""),
            status_category=str((status.get("statusCategory") or {}).get("key") or ""),
            priority_name=str((f.get("priority") or {}).get("name") or ""),
            issue_type=str((f.get("issuetype") or {}).get("name") or ""),
            project_key=str((f.get("project") or {}).get("key") or ""),
            assignee_account_id=str(assignee.get("accountId") or ""),
            assignee_name=str(assignee.get("displayName") or ""),
            reporter_account_id=str(reporter.get("accountId") or ""),
            reporter_name=str(reporter.get("displayName") or ""),
            reporter_email=str(reporter.get("emailAddress") or ""),
            created=str(f.get("created") or ""),
            updated=str(f.get("updated") or f.get("created") or ""),
            duedate=f.get("duedate"),
            labels=[str(x) for x in (f.get("labels") or [])],
            base_url=self.client.base_url,
            my_account_id=my_account_id,
            matched_scopes=[scope_name],
        )
