"""Command-bar filter matching (pure logic; no LLM/HTTP)."""
from email.utils import formatdate

from haven.routers import command as cmd


def _item(**kw):
    base = {"subject": "", "sender_email": "", "sender_name": "",
            "sender_domain": "", "tag": "fyi", "urgency": "low"}
    base.update(kw)
    return base


def test_sender_contains():
    it = _item(sender_email="approvals@ayarlabs.coupahost.com")
    assert cmd.match_item(it, {"sender_contains": "coupa"}) is True
    assert cmd.match_item(it, {"sender_contains": "zoom"}) is False


def test_tag_and_urgency():
    assert cmd.match_item(_item(tag="approval"), {"tag": "approval"}) is True
    assert cmd.match_item(_item(tag="fyi"), {"tag": "approval"}) is False
    assert cmd.match_item(_item(urgency="urgent"), {"urgency": "urgent"}) is True


def test_unhandled_only():
    assert cmd.match_item(_item(handled_at=123), {}) is False               # default skips handled
    assert cmd.match_item(_item(handled_at=123), {"unhandled_only": False}) is True


def test_older_and_newer_than_days():
    old = _item(date="Wed, 01 Jan 2020 00:00:00 +0000")
    assert cmd.match_item(old, {"older_than_days": 2}) is True
    assert cmd.match_item(old, {"newer_than_days": 2}) is False
    recent = _item(date=formatdate(usegmt=True))
    assert cmd.match_item(recent, {"older_than_days": 2}) is False
    # no date -> age unknown -> age-based filters never match (don't act blind)
    assert cmd.match_item(_item(), {"older_than_days": 2}) is False


def test_coerce_filter_drops_invalid():
    f = cmd._coerce_filter({"source": "gmail", "tag": "bogus",
                            "older_than_days": -5, "junk": 1, "watchlist": True})
    assert f["source"] == "gmail"
    assert "tag" not in f
    assert "older_than_days" not in f          # negative dropped
    assert f["watchlist"] is True
    assert f["unhandled_only"] is True          # defaults on
