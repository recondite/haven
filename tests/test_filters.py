"""Tests for the deterministic pre-LLM filter rules.

`apply_filter`, `is_blocked` and `watchlist_match` read config/state from disk, so
each test monkeypatches the loaders to keep the cases hermetic.
"""
import pytest

from haven import filters
from haven.filters import Decision


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Default: empty config, empty blocklist, empty watchlist. Tests override as needed."""
    monkeypatch.setattr(filters, "load_config", lambda: {})
    monkeypatch.setattr(filters, "_load_blocklist", lambda: {"senders": [], "domains": []})
    monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: [])


def _set_config(monkeypatch, cfg):
    monkeypatch.setattr(filters, "load_config", lambda: cfg)


class TestIsBlocked:
    def test_blocked_sender(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [{"email": "spam@x.com"}], "domains": []})
        blocked, reason = filters.is_blocked("spam@x.com", "x.com")
        assert blocked is True

    def test_blocked_domain(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [], "domains": [{"domain": "spam.com"}]})
        blocked, _ = filters.is_blocked("anyone@spam.com", "spam.com")
        assert blocked is True

    def test_not_blocked(self):
        assert filters.is_blocked("ok@x.com", "x.com")[0] is False


class TestWatchlistMatch:
    def test_subject_whole_word(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["permit"])
        assert filters.watchlist_match(subject="Need the permit today") == "permit"

    def test_subject_no_partial(self, monkeypatch):
        # whole-word: "permit" must NOT match "supermarket"
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["permit"])
        assert filters.watchlist_match(subject="supermarket run") is None

    def test_sender_substring(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["clearsulting"])
        assert filters.watchlist_match(sender_email="josh@clearsulting.com") == "clearsulting"

    def test_no_match(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["xyz"])
        assert filters.watchlist_match(subject="hello", sender_email="a@b.com") is None


class TestApplyFilter:
    def test_blocklist_highest_priority(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [{"email": "spam@x.com"}], "domains": []})
        decision, reason, flags = filters.apply_filter({"sender_email": "spam@x.com"})
        assert decision == Decision.REJECT

    def test_watchlist_force_keep(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["boardprep"])
        decision, reason, flags = filters.apply_filter(
            {"sender_email": "ext@vendor.com", "subject": "boardprep deck"}
        )
        assert decision == Decision.ACCEPT
        assert flags.get("watchlist_match") == "boardprep"

    def test_keep_domain_accept(self, monkeypatch):
        _set_config(monkeypatch, {"keep": {"domains": ["ayarlabs.com"]}})
        decision, reason, flags = filters.apply_filter(
            {"sender_email": "p@ayarlabs.com", "sender_domain": "ayarlabs.com"}
        )
        assert decision == Decision.ACCEPT
        assert flags.get("from_ayar") is True

    def test_elt_member_accept(self, monkeypatch):
        _set_config(monkeypatch, {"keep": {"elt": ["ceo@ayarlabs.com"]}})
        decision, _, flags = filters.apply_filter(
            {"sender_email": "ceo@ayarlabs.com", "sender_domain": "ayarlabs.com"}
        )
        assert decision == Decision.ACCEPT
        assert flags.get("is_elt") is True

    def test_never_keep_beats_keep_domain(self, monkeypatch):
        _set_config(monkeypatch, {
            "keep": {"domains": ["ayarlabs.com"]},
            "never_keep": {"subject_patterns": [r"out of office"]},
        })
        # never_keep is checked after label/watchlist but before keep-domain,
        # so an OOO from an ayarlabs sender is still rejected.
        decision, reason, _ = filters.apply_filter({
            "sender_email": "p@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "subject": "Out of Office: back Monday",
        })
        assert decision == Decision.REJECT

    def test_reject_subject_pattern(self, monkeypatch):
        _set_config(monkeypatch, {"reject": {"subject_patterns": [r"unsubscribe"]}})
        decision, _, _ = filters.apply_filter(
            {"sender_email": "x@ext.com", "sender_domain": "ext.com", "subject": "click to unsubscribe"}
        )
        assert decision == Decision.REJECT

    def test_uncertain_default(self, monkeypatch):
        decision, reason, _ = filters.apply_filter(
            {"sender_email": "x@unknown.com", "sender_domain": "unknown.com", "subject": "hello"}
        )
        assert decision == Decision.UNCERTAIN

    def test_ayar_person_on_cc_kept(self, monkeypatch):
        decision, reason, flags = filters.apply_filter({
            "sender_email": "ext@vendor.com", "sender_domain": "vendor.com",
            "subject": "proposal", "cc": [{"email": "garth@ayarlabs.com"}],
        })
        assert decision == Decision.ACCEPT
        assert flags.get("ayar_in_thread") is True

    def test_invalid_regex_does_not_crash(self, monkeypatch):
        _set_config(monkeypatch, {"reject": {"subject_patterns": ["[unclosed"]}})
        # Should log a warning and fall through to UNCERTAIN, not raise.
        decision, _, _ = filters.apply_filter(
            {"sender_email": "x@ext.com", "sender_domain": "ext.com", "subject": "anything"}
        )
        assert decision == Decision.UNCERTAIN
