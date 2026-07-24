"""Unit tests for the Asana source — pure logic, no network."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from haven.routers.asana import merge_prev
from haven.sources.asana import AsanaFetcher, AsanaItem


def run(coro):
    return asyncio.run(coro)


def _item(**over) -> AsanaItem:
    base = dict(
        gid="1001",
        name="Ship the DP roadmap update",
        notes="Refresh the roadmap deck.",
        due_at=None,
        due_on=None,
        permalink_url="https://app.asana.com/0/0/1001",
        assignee_gid="user-9",
        assignee_name="Jeffrey Cotter",
        projects=["Data Pillars"],
        created_at="2026-07-01T10:00:00.000Z",
        modified_at="2026-07-20T10:00:00.000Z",
    )
    base.update(over)
    return AsanaItem(**base)


# ─── urgency ─────────────────────────────────────────────

def test_urgency_no_due_is_low():
    assert _item().summary()["urgency"] == "low"


def test_urgency_overdue_is_urgent():
    y = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    p = _item(due_on=y).summary()
    assert p["sla_breached"] is True
    assert p["urgency"] == "urgent"


def test_urgency_due_soon_is_high():
    t = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    p = _item(due_at=t).summary()
    assert p["sla_at_risk"] is True
    assert p["urgency"] == "high"


def test_urgency_due_later_is_med():
    later = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
    assert _item(due_on=later).summary()["urgency"] == "med"


# ─── payload contract ────────────────────────────────────

def test_payload_contract():
    p = _item(due_on="2026-08-01").summary()
    for k in ("source", "msg_id", "subject", "sender_name", "date", "deeplink",
              "task_gid", "assignee_gid", "assignee_name", "project_name",
              "due_by", "tag", "urgency", "action_required", "summary"):
        assert k in p, f"missing {k}"
    assert p["source"] == "asana"
    assert p["msg_id"] == "asana:1001"
    assert p["subject"] == "[Data Pillars] Ship the DP roadmap update"
    assert p["assignee_gid"] == "user-9"
    assert p["tag"] == "action"
    assert p["sender_email"] == ""            # attribution is via gid, not email


def test_subject_without_project():
    assert _item(projects=[]).summary()["subject"] == "Ship the DP roadmap update"


# ─── fetcher: completed filter, never_keep, sort ─────────

class StubClient:
    def __init__(self, tasks):
        self.tasks = tasks

    async def me(self):
        return {"gid": "me", "workspace_gid": "ws1"}

    async def tasks_assigned_to_me(self, *, page_size, max_pages):
        return self.tasks

    async def aclose(self):
        pass


def _raw(gid, **over):
    d = {"gid": gid, "name": f"task {gid}", "notes": "", "completed": False,
         "permalink_url": f"https://app.asana.com/0/0/{gid}",
         "assignee": {"gid": "u1", "name": "A"}, "projects": [{"name": "Proj"}],
         "created_at": "2026-07-01T10:00:00.000Z",
         "modified_at": f"2026-07-{gid[-2:]}T10:00:00.000Z"}
    d.update(over)
    return d


def test_fetch_drops_completed_and_never_keep():
    f = AsanaFetcher(client=StubClient([
        _raw("1010"),
        _raw("1011", completed=True),                       # dropped
        _raw("1012", projects=[{"name": "Personal"}]),      # never_keep
    ]))
    f.never_keep_projects = {"personal"}
    items = run(f.fetch_all())
    assert [i.gid for i in items] == ["1010"]


def test_fetch_sorts_recent_first():
    f = AsanaFetcher(client=StubClient([_raw("1010"), _raw("1099")]))
    f.never_keep_projects = set()
    items = run(f.fetch_all())
    assert [i.gid for i in items] == ["1099", "1010"]       # modified desc


# ─── router prev-merge + reactivation ────────────────────

def test_merge_prev_preserves_local_state():
    p = _item().summary()
    prev = {"handled_at": time.time() + 3600, "snooze_until": 5.0, "linear_id": "L1"}
    merge_prev(p, prev)
    assert p["handled_at"] == prev["handled_at"]
    assert p["snooze_until"] == 5.0 and p["linear_id"] == "L1"


def test_merge_prev_reactivates_on_newer_modify():
    p = _item().summary()   # modified 2026-07-20
    merge_prev(p, {"handled_at": datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()})
    assert "handled_at" not in p
