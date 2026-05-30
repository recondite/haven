"""Tests for Linear payload-shaping helpers (pure functions)."""
from haven import linear


class TestPriorityFromPayload:
    def test_urgency_map(self):
        assert linear._priority_from_payload({"urgency": "urgent"}) == 1
        assert linear._priority_from_payload({"urgency": "high"}) == 2
        assert linear._priority_from_payload({"urgency": "med"}) == 3
        assert linear._priority_from_payload({"urgency": "low"}) == 4

    def test_default_low(self):
        assert linear._priority_from_payload({}) == 4

    def test_action_required_bumps_priority(self):
        # high (2) + action_required -> 1
        assert linear._priority_from_payload({"urgency": "high", "action_required": True}) == 1

    def test_action_required_does_not_exceed_urgent(self):
        assert linear._priority_from_payload({"urgency": "urgent", "action_required": True}) == 1


class TestBuildTitle:
    def test_uses_subject(self):
        assert linear._build_title({"subject": "Fix the thing"}) == "Fix the thing"

    def test_falls_back_to_suggested_action(self):
        assert linear._build_title({"suggested_action": "Reply to vendor"}) == "Reply to vendor"

    def test_untitled_default(self):
        assert linear._build_title({}) == "Untitled"

    def test_truncates_long_title(self):
        t = linear._build_title({"subject": "x" * 300})
        assert len(t) == 200
        assert t.endswith("...")


class TestBuildDescription:
    def test_includes_sender_and_source(self):
        desc = linear._build_description({
            "sender_name": "Alice", "sender_email": "a@x.com",
            "sender_company": "X", "deeplink": "https://mail/x", "summary": "Hi",
        })
        assert "Alice <a@x.com>" in desc
        assert "(X)" in desc
        assert "https://mail/x" in desc
        assert "Captured by Haven" in desc

    def test_suggested_reply_only_when_reply_needed(self):
        without = linear._build_description({"suggested_reply": "Sure", "reply_needed": False})
        assert "Sure" not in without
        with_reply = linear._build_description({"suggested_reply": "Sure", "reply_needed": True})
        assert "Sure" in with_reply
