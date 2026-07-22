"""Unit tests for the Jira source — pure logic, no network."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from haven.routers.jira import merge_prev
from haven.sources.jira import JiraFetcher, JiraItem, _adf_text


def run(coro):
    return asyncio.run(coro)


def _item(**over) -> JiraItem:
    base = dict(
        key="IT-1",
        summary_text="Okta group for tapeout share",
        description_text="Please create the group.",
        status_name="In Progress",
        status_category="indeterminate",
        priority_name="Medium",
        issue_type="Task",
        project_key="IT",
        assignee_account_id="me-123",
        assignee_name="Garth Thompson",
        reporter_account_id="acc-9",
        reporter_name="Priya Sharma",
        reporter_email="priya@ayarlabs.com",
        created="2026-07-01T10:00:00.000-0700",
        updated="2026-07-20T10:00:00.000-0700",
        duedate=None,
        labels=[],
        base_url="https://ayarlabs.atlassian.net",
        my_account_id="me-123",
        matched_scopes=["assigned"],
    )
    base.update(over)
    return JiraItem(**base)


# ─── urgency ─────────────────────────────────────────────

@pytest.mark.parametrize("priority,expected", [
    ("Highest", "urgent"), ("High", "high"), ("Medium", "med"),
    ("Low", "low"), ("Lowest", "low"), ("", "low"),
])
def test_urgency_priority_map(priority, expected):
    assert _item(priority_name=priority).summary()["urgency"] == expected


def test_urgency_due_past_forces_urgent():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    p = _item(priority_name="Low", duedate=yesterday).summary()
    assert p["sla_breached"] is True
    assert p["urgency"] == "urgent"


def test_urgency_due_soon_bumps_one_step():
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    p = _item(priority_name="Medium", duedate=tomorrow).summary()
    assert p["sla_at_risk"] is True
    assert p["urgency"] == "high"


# ─── payload contract ────────────────────────────────────

def test_payload_contract_keys():
    p = _item().summary()
    for k in (
        "source", "msg_id", "subject", "sender_name", "sender_email", "date",
        "deeplink", "snippet", "body_text", "labels",
        "issue_key", "status_label", "status_category", "priority_label",
        "issue_type", "project_key", "matched_scopes", "due_by",
        "sla_breached", "sla_at_risk",
        "tag", "urgency", "action_required", "summary", "suggested_action",
    ):
        assert k in p, f"missing payload key {k}"
    assert p["source"] == "jira"
    assert p["msg_id"] == "jira:IT-1"
    assert p["subject"] == "[IT-1] Okta group for tapeout share"
    assert p["deeplink"] == "https://ayarlabs.atlassian.net/browse/IT-1"
    assert p["is_assigned_to_me"] is True
    assert p["tag"] == "action"
    assert p["action_required"] is True


def test_approval_scope_tags_approval():
    p = _item(matched_scopes=["pending_approvals"]).summary()
    assert p["tag"] == "approval"
    assert p["suggested_action"] == "Approve or reject"


def test_redacted_reporter_email_ok():
    p = _item(reporter_email="", reporter_name="").summary()
    assert p["sender_email"] == ""
    assert p["sender_name"] == "Unknown reporter"


# ─── fetcher: scope union, dedup, never_keep ─────────────

class StubClient:
    base_url = "https://ayarlabs.atlassian.net"

    def __init__(self, by_jql: dict[str, list[dict]]):
        self.by_jql = by_jql

    async def myself(self) -> str:
        return "me-123"

    async def search_jql(self, jql, *, max_results=100, next_page_token=None):
        return {"issues": self.by_jql.get(jql, [])}

    async def aclose(self) -> None:
        pass


def _raw(key: str, **fields) -> dict:
    f = {
        "summary": "s", "status": {"name": "Open", "statusCategory": {"key": "new"}},
        "priority": {"name": "Medium"}, "issuetype": {"name": "Task"},
        "project": {"key": key.split("-")[0]},
        "assignee": None, "reporter": None,
        "created": "2026-07-01T10:00:00.000-0700",
        "updated": "2026-07-20T10:00:00.000-0700",
        "duedate": None, "description": None, "labels": [],
    }
    f.update(fields)
    return {"key": key, "fields": f}


def test_scope_union_dedup_merges_matched_scopes():
    f = JiraFetcher(client=StubClient({
        "JQL_A": [_raw("IT-1"), _raw("IT-2")],
        "JQL_B": [_raw("IT-2"), _raw("IT-3")],
    }))
    f.scopes = [
        {"name": "assigned", "enabled": True, "jql": "JQL_A"},
        {"name": "watched_stale", "enabled": True, "jql": "JQL_B"},
    ]
    f.never_keep_types, f.never_keep_statuses = set(), set()
    items = run(f.fetch_all())
    by_key = {i.key: i for i in items}
    assert set(by_key) == {"IT-1", "IT-2", "IT-3"}
    assert by_key["IT-2"].matched_scopes == ["assigned", "watched_stale"]


def test_bad_scope_skipped_not_fatal():
    class FailingClient(StubClient):
        async def search_jql(self, jql, *, max_results=100, next_page_token=None):
            if jql == "BAD":
                raise RuntimeError("Jira POST /rest/api/3/search/jql HTTP 400: bad JQL")
            return await super().search_jql(jql, max_results=max_results)

    f = JiraFetcher(client=FailingClient({"GOOD": [_raw("IT-1")]}))
    f.scopes = [
        {"name": "pending_approvals", "enabled": True, "jql": "BAD"},
        {"name": "assigned", "enabled": True, "jql": "GOOD"},
    ]
    f.never_keep_types, f.never_keep_statuses = set(), set()
    items = run(f.fetch_all())
    assert [i.key for i in items] == ["IT-1"]


def test_never_keep_filters():
    f = JiraFetcher(client=StubClient({
        "Q": [_raw("IT-1", issuetype={"name": "Sub-task"}), _raw("IT-2")],
    }))
    f.scopes = [{"name": "assigned", "enabled": True, "jql": "Q"}]
    f.never_keep_types, f.never_keep_statuses = {"sub-task"}, set()
    items = run(f.fetch_all())
    assert [i.key for i in items] == ["IT-2"]


# ─── router: prev-merge + reactivation ───────────────────

def test_merge_prev_preserves_local_state():
    payload = _item().summary()
    prev = {"handled_at": time.time() + 3600, "snooze_until": 123.0, "linear_id": "lin-1"}
    merge_prev(payload, prev)
    # handled_at is in the future relative to updated → survives
    assert payload["handled_at"] == prev["handled_at"]
    assert payload["snooze_until"] == 123.0
    assert payload["linear_id"] == "lin-1"


def test_merge_prev_reactivates_on_newer_update():
    # Issue updated 2026-07-20; Garth marked it done back in 2025 → resurface.
    payload = _item().summary()
    merge_prev(payload, {"handled_at": datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()})
    assert "handled_at" not in payload


def test_merge_prev_none_is_noop():
    payload = _item().summary()
    assert merge_prev(payload, None) is payload


# ─── ADF extraction ──────────────────────────────────────

def test_adf_text():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Please create"},
                {"type": "text", "text": "the group."},
            ]},
        ],
    }
    assert _adf_text(adf) == "Please create the group."
    assert _adf_text("plain string") == "plain string"
    assert _adf_text(None) == ""
