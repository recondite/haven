"""Linear GraphQL client — create issues for AR capture (Phase 1.6).

Per ground rules: only non-destructive writes. We never call `issueDelete` or
`issueArchive`. The 30s "undo" is implemented entirely client-side: the UI
delays POSTing here for 30s and cancels locally if the user clicks Undo, so
no Linear mutation runs at all on undo.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import httpx

from haven import config

log = logging.getLogger(__name__)

LINEAR_URL = "https://api.linear.app/graphql"

# Linear priority enum: 0=none, 1=urgent, 2=high, 3=med, 4=low.
URGENCY_TO_PRIORITY = {
    "urgent": 1,
    "high": 2,
    "med": 3,
    "low": 4,
}


class LinearError(RuntimeError):
    pass


_team_id_cache: str | None = None


async def _gql(query: str, variables: dict[str, Any]) -> dict:
    if not config.LINEAR_API_KEY:
        raise LinearError("LINEAR_API_KEY missing in .env")
    headers = {
        "Authorization": config.LINEAR_API_KEY,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            LINEAR_URL,
            headers=headers,
            json={"query": query, "variables": variables},
        )
    if r.status_code != 200:
        raise LinearError(f"Linear HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("errors"):
        raise LinearError(f"Linear GraphQL error: {body['errors']}")
    return body["data"]


async def team_id_for_project(project_id: str) -> str:
    """Discover the team that owns the configured project. Cached in-process."""
    global _team_id_cache
    if _team_id_cache:
        return _team_id_cache
    data = await _gql(
        """
        query ($id: String!) {
          project(id: $id) {
            teams { nodes { id key name } }
          }
        }
        """,
        {"id": project_id},
    )
    teams = (data.get("project") or {}).get("teams", {}).get("nodes") or []
    if not teams:
        raise LinearError(f"No teams found for Linear project {project_id}")
    _team_id_cache = teams[0]["id"]
    log.info("Linear team resolved: %s (%s)", teams[0].get("key"), teams[0]["id"])
    return _team_id_cache


def _priority_from_payload(payload: dict) -> int:
    urgency = (payload.get("urgency") or "low").lower()
    pri = URGENCY_TO_PRIORITY.get(urgency, 4)
    if payload.get("action_required") and pri > 1:
        pri -= 1
    return pri


def _due_date_iso(days_ahead: int = 7) -> str:
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def _build_title(payload: dict) -> str:
    title = (payload.get("subject") or payload.get("suggested_action") or "Untitled").strip()
    if len(title) > 200:
        title = title[:197] + "..."
    return title


def _build_description(payload: dict) -> str:
    sender = payload.get("sender_name") or ""
    sender_email = payload.get("sender_email") or ""
    sender_company = payload.get("sender_company") or payload.get("sender_domain") or ""
    deeplink = payload.get("deeplink") or ""
    summary = payload.get("summary") or ""
    suggested = payload.get("suggested_action") or ""
    reply_needed = payload.get("reply_needed")
    suggested_reply = payload.get("suggested_reply") or ""

    parts: list[str] = []
    parts.append(f"**From:** {sender} <{sender_email}>" + (f" ({sender_company})" if sender_company else ""))
    if deeplink:
        parts.append(f"**Source:** [Open in Gmail]({deeplink})")
    if summary:
        parts.append(f"\n**Summary**\n{summary}")
    if suggested:
        parts.append(f"\n**Suggested action**\n{suggested}")
    if reply_needed and suggested_reply:
        parts.append(f"\n**Suggested reply**\n> {suggested_reply}")
    parts.append("\n---\n*Captured by Haven from Gmail.*")
    return "\n".join(parts)


async def create_issue_from_email(payload: dict) -> dict:
    """Create a Linear issue from a cached Gmail payload. Returns the issue node."""
    if not config.LINEAR_PROJECT_ID:
        raise LinearError("LINEAR_PROJECT_ID missing in .env")

    team_id = await team_id_for_project(config.LINEAR_PROJECT_ID)

    input_obj = {
        "teamId": team_id,
        "projectId": config.LINEAR_PROJECT_ID,
        "title": _build_title(payload),
        "description": _build_description(payload),
        "priority": _priority_from_payload(payload),
        "dueDate": _due_date_iso(7),
    }

    data = await _gql(
        """
        mutation ($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { id identifier url title priority }
          }
        }
        """,
        {"input": input_obj},
    )
    res = data.get("issueCreate") or {}
    if not res.get("success") or not res.get("issue"):
        raise LinearError(f"issueCreate did not succeed: {data}")
    issue = res["issue"]
    log.info("Linear issue created: %s %s", issue.get("identifier"), issue.get("url"))
    return issue
