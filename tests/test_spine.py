"""Spine dual-write: payload -> item row mapping and diff parity."""
from haven.spine import Spine


def _spine(tmp_path):
    return Spine(tmp_path / "spine.sqlite")


def test_upsert_projects_payload(tmp_path):
    s = _spine(tmp_path)
    s.upsert_item("gmail", "m1", {
        "subject": "Q3 budget", "sender": "cfo@ayarlabs.com", "tag": "approval",
        "thread_id": "t1", "score": 0.9,
    })
    row = s.get_item("gmail", "m1")
    assert row["kind"] == "message"
    assert row["subject"] == "Q3 budget"
    assert row["sender"] == "cfo@ayarlabs.com"
    assert row["tags"] == "approval"
    assert row["status"] == "open"
    assert row["thread_id"] == "t1"


def test_status_derivation(tmp_path):
    s = _spine(tmp_path)
    s.upsert_item("otter", "a1", {"handled_at": 123.0})
    assert s.get_item("otter", "a1")["status"] == "handled"
    assert s.get_item("otter", "a1")["kind"] == "ar"
    s.upsert_item("slack", "c:ts", {"snooze_until": 999.0})
    assert s.get_item("slack", "c:ts")["status"] == "snoozed"


def test_upsert_preserves_first_seen_bumps_last(tmp_path):
    s = _spine(tmp_path)
    s.upsert_item("gmail", "m1", {"subject": "v1"})
    first = s.get_item("gmail", "m1")["first_seen"]
    s.upsert_item("gmail", "m1", {"subject": "v2"})
    row = s.get_item("gmail", "m1")
    assert row["subject"] == "v2"
    assert row["first_seen"] == first  # unchanged on update


def test_diff_clean_after_upsert(tmp_path):
    s = _spine(tmp_path)
    payloads = {
        "m1": {"subject": "a", "sender": "x@y.com", "tag": "fyi", "thread_id": "t1"},
        "m2": {"subject": "b", "handled_at": 5.0},
    }
    for ext_id, p in payloads.items():
        s.upsert_item("gmail", ext_id, p)
    assert s.diff_source(payloads, "gmail") == []


def test_diff_flags_missing_and_mismatch(tmp_path):
    s = _spine(tmp_path)
    s.upsert_item("gmail", "m1", {"subject": "stale"})
    cached = {
        "m1": {"subject": "fresh"},          # field_mismatch
        "m2": {"subject": "never written"},  # missing_in_item
    }
    reasons = {d["external_id"]: d["reason"] for d in s.diff_source(cached, "gmail")}
    assert reasons == {"m1": "field_mismatch", "m2": "missing_in_item"}
