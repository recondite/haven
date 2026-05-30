"""Tests for cross-source contact derivation (pure function)."""
from haven import contacts


def _gmail(email, name="", company="", **extra):
    return {"source": "gmail", "sender_email": email, "sender_name": name,
            "sender_company": company, "msg_id": extra.pop("msg_id", f"g:{email}"), **extra}


class TestDeriveContacts:
    def test_basic_aggregation(self):
        items = [
            _gmail("a@x.com", "Alice", "X Corp", date="2026-01-01", msg_id="g1"),
            _gmail("a@x.com", "Alice", "X Corp", date="2026-02-01", msg_id="g2"),
        ]
        out = contacts.derive_contacts(items)
        assert len(out) == 1
        c = out[0]
        assert c.email == "a@x.com"
        assert c.counts_by_source["gmail"] == 2
        assert c.first_seen == "2026-01-01"
        assert c.last_seen == "2026-02-01"
        assert c.open_count == 2

    def test_self_excluded(self):
        items = [_gmail("garth@ayarlabs.com", "Garth")]
        out = contacts.derive_contacts(items, self_email="garth@ayarlabs.com")
        assert out == []

    def test_handled_vs_open(self):
        items = [
            _gmail("a@x.com", msg_id="g1", handled_at=123),
            _gmail("a@x.com", msg_id="g2", linear_id="L1"),
            _gmail("a@x.com", msg_id="g3"),
        ]
        c = contacts.derive_contacts(items)[0]
        assert c.handled_count == 2
        assert c.open_count == 1

    def test_owes_reply_gmail_only(self):
        items = [
            _gmail("a@x.com", msg_id="g1", last_inbound_at="d1", garth_owns_last_turn=False),
        ]
        c = contacts.derive_contacts(items)[0]
        assert c.owes_reply_count == 1

    def test_slack_without_email_skipped(self):
        items = [{"source": "slack", "sender_email": "", "sender_name": "Bob", "msg_id": "s1"}]
        assert contacts.derive_contacts(items) == []

    def test_otter_guests_excluding_self(self):
        items = [{
            "source": "otter", "msg_id": "o1", "date": "2026-03-01",
            "calendar_guest_emails": ["guest@x.com", "garth@ayarlabs.com"],
        }]
        out = contacts.derive_contacts(items, self_email="garth@ayarlabs.com")
        assert [c.email for c in out] == ["guest@x.com"]

    def test_longest_name_wins(self):
        items = [
            _gmail("a@x.com", "A", msg_id="g1"),
            _gmail("a@x.com", "Alice Smith", msg_id="g2"),
        ]
        assert contacts.derive_contacts(items)[0].name == "Alice Smith"
