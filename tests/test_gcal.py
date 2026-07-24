"""Unit tests for the 1:1 calendar picker — pure logic, no network."""
from __future__ import annotations

from haven.sources.gcal import pick_one_on_one


def _ev(summary, start="2026-07-25T15:00:00-07:00", **over):
    e = {"summary": summary, "start": {"dateTime": start}}
    e.update(over)
    return e


def test_matches_slash_title_and_returns_earliest():
    events = [
        _ev("Garth / Priya Sharma", start="2026-07-25T15:00:00-07:00"),
        _ev("Priya 1:1", start="2026-07-24T15:00:00-07:00"),
    ]
    # events are pre-sorted upcoming; first match wins (caller sorts by startTime)
    r = pick_one_on_one(events, "Priya Sharma")
    assert r["summary"] == "Garth / Priya Sharma"
    assert r["next_time"] == "2026-07-25T15:00:00-07:00"


def test_matches_1on1_marker():
    r = pick_one_on_one([_ev("Priya <> Garth weekly")], "Priya Sharma")
    assert r is not None


def test_ignores_unrelated_events():
    events = [_ev("Team standup"), _ev("Lunch with Bob"), _ev("Board meeting")]
    assert pick_one_on_one(events, "Priya Sharma") is None


def test_requires_person_name_not_just_marker():
    # A 1:1 with someone else must not match Priya.
    assert pick_one_on_one([_ev("Bob 1:1")], "Priya Sharma") is None


def test_extracts_doc_attachment():
    ev = _ev("Priya 1:1", attachments=[
        {"mimeType": "application/pdf", "fileUrl": "https://x/y.pdf"},
        {"mimeType": "application/vnd.google-apps.document", "fileUrl": "https://docs.google.com/document/d/ABC/edit"},
    ])
    r = pick_one_on_one([ev], "Priya Sharma")
    assert r["doc_url"] == "https://docs.google.com/document/d/ABC/edit"


def test_extracts_doc_link_from_description():
    ev = _ev("Priya 1:1", description="Agenda: https://docs.google.com/document/d/XYZ/edit please review")
    r = pick_one_on_one([ev], "Priya Sharma")
    assert r["doc_url"] == "https://docs.google.com/document/d/XYZ/edit"


def test_no_doc_is_empty_string():
    r = pick_one_on_one([_ev("Priya 1:1")], "Priya Sharma")
    assert r["doc_url"] == ""


def test_all_day_event_start_uses_date():
    ev = {"summary": "Priya 1:1", "start": {"date": "2026-07-25"}}
    r = pick_one_on_one([ev], "Priya Sharma")
    assert r["next_time"] == "2026-07-25"
