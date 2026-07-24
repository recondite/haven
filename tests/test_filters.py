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


class TestGarthAdded:
    @pytest.mark.parametrize("text", [
        "Adding Garth for visibility",
        "Looping in Garth on this one",
        "Pulling Garth into the thread",
        "cc Garth",
        "Garth, looping you in",
        "Hi team — adding you to this thread",
        "Looping you in Garth",
        "+Garth",
        "@garth can you weigh in",
    ])
    def test_matches(self, text):
        assert filters.garth_added_match(text) is not None

    @pytest.mark.parametrize("text", [
        "The quarterly report is attached",
        "Gareth from vendor will follow up",   # not 'garth', no add-verb+you
        "Garth's calendar is full",            # mentions name, no add verb
    ])
    def test_no_false_match(self, text):
        assert filters.garth_added_match(text) is None

    def test_scans_body_not_just_subject(self):
        assert filters.garth_added_match("Re: PDK", "", "Hey, adding Garth so he can approve") is not None

    def test_apply_filter_flags_and_accepts(self, monkeypatch):
        payload = {"subject": "Re: tapeout", "snippet": "looping you in on the budget", "body_text": ""}
        decision, reason, flags = filters.apply_filter(payload)
        assert decision == Decision.ACCEPT
        assert "garth_added" in flags


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

    def test_domain_prefix_substring(self, monkeypatch):
        # "ayar" should still catch "ayarlabs.com" (domain substring, prefix ok).
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["ayar"])
        assert filters.watchlist_match(sender_email="x@ayarlabs.com") == "ayar"

    def test_local_part_gibberish_no_false_positive(self, monkeypatch):
        # "JLL" buried in a random no-reply local part must NOT match.
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["JLL"])
        assert filters.watchlist_match(
            subject="You are out of seats for your Claude Team plan",
            sender_email="no-reply-fchcfojll7yiqstyifvioa@mail.anthropic.com",
            sender_domain="mail.anthropic.com",
            sender_name="Anthropic",
        ) is None

    def test_real_jll_domain_matches(self, monkeypatch):
        # But a genuine JLL sender (own domain) still matches.
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["JLL"])
        assert filters.watchlist_match(sender_email="broker@jll.com") == "JLL"

    def test_no_match(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["xyz"])
        assert filters.watchlist_match(subject="hello", sender_email="a@b.com") is None


class TestApplyFilter:
    def test_blocklist_highest_priority(self, monkeypatch):
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [{"email": "spam@x.com"}], "domains": []})
        decision, reason, flags = filters.apply_filter({"sender_email": "spam@x.com"})
        assert decision == Decision.REJECT

    def test_ignore_label_rejected(self, monkeypatch):
        # The Gmail "ignore" label always rejects, regardless of case.
        decision, reason, _ = filters.apply_filter(
            {"sender_email": "lisa@ayarlabs.com", "sender_domain": "ayarlabs.com",
             "labels": ["INBOX", "Ignore"]}
        )
        assert decision == Decision.REJECT
        assert reason == "ignore label"

    def test_ignore_label_beats_watchlist(self, monkeypatch):
        # Ignore is checked before watchlist, so it wins even on a watchlist hit.
        monkeypatch.setattr(filters, "_load_watchlist_raw", lambda: ["boardprep"])
        decision, _, _ = filters.apply_filter(
            {"sender_email": "ext@vendor.com", "subject": "boardprep deck",
             "labels": ["ignore"]}
        )
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


class TestKeepOnlyIfDirect:
    """it-helpdesk / Freshservice blasts: kept only when Garth is a direct To:."""

    CFG = {
        "self_email": "garth@ayarlabs.com",
        "keep_only_if_direct": {"senders": ["it-helpdesk@ayarlabs.com"]},
        # ayarlabs.com would otherwise auto-accept — proves the rule runs first.
        "keep": {"domains": ["ayarlabs.com"]},
    }

    def test_cc_only_rejected_via_to_list(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, reason, _ = filters.apply_filter({
            "sender_email": "it-helpdesk@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "subject": "Re: Lactation Room",
            "to": [{"email": "someone@ayarlabs.com"}],
            "cc": [{"email": "garth@ayarlabs.com"}],
        })
        assert decision == Decision.REJECT
        assert "directly addressed" in reason

    def test_direct_to_kept_via_to_list(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "it-helpdesk@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "subject": "[ IT ] New ticket has been created",
            "to": [{"email": "garth@ayarlabs.com"}],
        })
        assert decision == Decision.ACCEPT
        assert flags.get("direct_to_garth") is True

    def test_uses_recipient_role_when_present(self, monkeypatch):
        # Cached/refiltered payloads carry garth_recipient_role instead of to/cc.
        _set_config(monkeypatch, self.CFG)
        rejected, _, _ = filters.apply_filter({
            "sender_email": "it-helpdesk@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "garth_recipient_role": "cc",
        })
        kept, _, _ = filters.apply_filter({
            "sender_email": "it-helpdesk@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "garth_recipient_role": "to",
        })
        assert rejected == Decision.REJECT
        assert kept == Decision.ACCEPT

    def test_block_still_wins(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [{"email": "it-helpdesk@ayarlabs.com"}], "domains": []})
        decision, _, _ = filters.apply_filter({
            "sender_email": "it-helpdesk@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "to": [{"email": "garth@ayarlabs.com"}],
        })
        assert decision == Decision.REJECT

    def test_other_ayar_sender_unaffected(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "lisa@ayarlabs.com", "sender_domain": "ayarlabs.com",
            "cc": [{"email": "garth@ayarlabs.com"}],
        })
        assert decision == Decision.ACCEPT  # normal ayarlabs.com keep still applies


class TestGoogleDriveShares:
    """Google Drive share / access-request notifications must be rejected even
    though google.com is in noreply_allowlist_domains — never_keep runs first."""

    # Mirror the real gmail.yaml rules under test.
    CFG = {
        "never_keep": {
            "sender_patterns": [r"\bdrive-shares[\w.+-]*@"],
            "subject_patterns": [
                r"\bshared (?:a|an|the|\d+|') .*with you\b",
                r"\bshared '.*' with you\b",
                r"\bhas shared\b.*\bwith you\b",
                r"^Invitation to (?:edit|view|comment)\b",
                r"\b(?:is )?request(?:ed|ing)? access to\b",
            ],
        },
        "keep": {"noreply_allowlist_domains": ["google.com"]},
    }

    def test_share_sender_rejected_despite_allowlist(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, reason, _ = filters.apply_filter({
            "sender_email": "drive-shares-dm-noreply@google.com",
            "sender_domain": "google.com",
            "subject": "Garth Thompson shared \"Q3 Plan\" with you",
        })
        assert decision == Decision.REJECT
        assert "never-keep" in reason

    def test_docs_share_sender_rejected(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, _, _ = filters.apply_filter({
            "sender_email": "drive-shares-noreply@docs.google.com",
            "sender_domain": "docs.google.com",
            "subject": "Invitation to edit",
        })
        assert decision == Decision.REJECT

    def test_access_request_subject_rejected(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, _, _ = filters.apply_filter({
            "sender_email": "drive-shares-dm-noreply@google.com",
            "sender_domain": "google.com",
            "subject": "Jane Doe is requesting access to Roadmap",
        })
        assert decision == Decision.REJECT

    def test_subject_backstop_rejects_non_drive_sender(self, monkeypatch):
        # Even if Google changes the From address, the subject backstop catches it.
        _set_config(monkeypatch, self.CFG)
        decision, _, _ = filters.apply_filter({
            "sender_email": "notify@google.com", "sender_domain": "google.com",
            "subject": "Alice shared 'Q4 Budget' with you",
        })
        assert decision == Decision.REJECT

    def test_human_email_not_falsely_rejected(self, monkeypatch):
        # A real person from an allowlisted-but-not-drive sender shouldn't be
        # caught; non-share Google notifications still defer to the LLM.
        _set_config(monkeypatch, self.CFG)
        decision, _, _ = filters.apply_filter({
            "sender_email": "calendar-notification@google.com",
            "sender_domain": "google.com",
            "subject": "Notification: Standup",
        })
        assert decision == Decision.UNCERTAIN  # noreply allowlist defers to LLM


class TestTravel:
    """Airline/hotel/car-rental confirmations are force-kept and flagged
    is_travel, even though their noreply senders would otherwise be rejected."""

    CFG = {
        "travel": {
            "domains": ["cathaypacific.com", "marriott.com", "hertz.com"],
            "subject_patterns": [
                r"\bitinerary\b",
                r"\bbooking confirmation\b",
                r"\bhotel (?:confirmation|reservation|booking)\b",
            ],
        },
        # Prove travel runs before these noreply rejects and would-be receipts.
        "reject": {"sender_patterns": [r"(?:^|[\w.+-]*[-_.])(?:noreply|no-reply)@"]},
        "never_keep": {"subject_patterns": [r"^Your .* receipt\b"]},
    }

    def test_travel_domain_kept_despite_noreply(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, reason, flags = filters.apply_filter({
            "sender_email": "noreply@cathaypacific.com",
            "sender_domain": "cathaypacific.com",
            "subject": "Your booking is confirmed",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_travel") is True
        assert "travel domain" in reason

    def test_travel_subdomain_matches(self, monkeypatch):
        # email.cathaypacific.com should match the cathaypacific.com entry.
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "cx@email.cathaypacific.com",
            "sender_domain": "email.cathaypacific.com",
            "subject": "anything",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_travel") is True

    def test_travel_subject_on_unknown_domain(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "noreply@some-airline.example",
            "sender_domain": "some-airline.example",
            "subject": "Your itinerary for trip to Tokyo",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_travel") is True

    def test_travel_beats_receipt_never_keep(self, monkeypatch):
        # A hotel confirmation also matching a receipt never_keep pattern should
        # still be kept as travel (travel runs first).
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "noreply@marriott.com",
            "sender_domain": "marriott.com",
            "subject": "Your Marriott receipt and hotel confirmation",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_travel") is True

    def test_blocklist_still_beats_travel(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [{"email": "noreply@cathaypacific.com"}], "domains": []})
        decision, _, _ = filters.apply_filter({
            "sender_email": "noreply@cathaypacific.com",
            "sender_domain": "cathaypacific.com",
            "subject": "Your booking confirmation",
        })
        assert decision == Decision.REJECT

    def test_non_travel_noreply_still_rejected(self, monkeypatch):
        # Control: a non-travel noreply with no travel signal is still rejected.
        _set_config(monkeypatch, self.CFG)
        decision, _, _ = filters.apply_filter({
            "sender_email": "noreply@randomvendor.com",
            "sender_domain": "randomvendor.com",
            "subject": "Check out our new features",
        })
        assert decision == Decision.REJECT


class TestUrgentApprovals:
    """Coupa (and similar) approval requests are force-kept and flagged
    is_priority_approval; the poll pipeline pins them tag=approval/urgency=urgent."""

    CFG = {
        "urgent_approvals": {
            "domains": ["coupahost.com", "coupa.com"],
            "approval_senders": ["approvals"],
            "subject_patterns": [
                r"\bapprov",
                r"\baction required\b",
                r"\b(?:requires|need(?:s|ed)?|pending|awaiting) (?:your )?(?:approval|review|sign-?off)\b",
                r"\brequisition\b.*\bapprov",
                r"\bpurchase order\b.*\bapprov",
            ],
        },
        # Coupa sends from do_not_reply@ — prove approval detection runs before reject.
        "reject": {"sender_patterns": [r"(?:^|[\w.+-]*[-_.])(?:noreply|no-reply)@"]},
    }

    def test_coupa_approval_kept_and_flagged(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, reason, flags = filters.apply_filter({
            "sender_email": "do_not_reply@coupahost.com",
            "sender_domain": "coupahost.com",
            "subject": "Action Required: Approve Requisition #4471",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_priority_approval") is True

    def test_coupa_subdomain_approval(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "no-reply@ayarlabs.coupahost.com",
            "sender_domain": "ayarlabs.coupahost.com",
            "subject": "REQ-22 requires your approval",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_priority_approval") is True

    def test_coupa_approvals_mailbox_flagged_regardless_of_subject(self, monkeypatch):
        # Real Coupa case: approvals@<tenant>.coupahost.com is always an approval
        # request, even if the subject has no approval keyword.
        _set_config(monkeypatch, self.CFG)
        decision, _, flags = filters.apply_filter({
            "sender_email": "approvals@ayarlabs.coupahost.com",
            "sender_domain": "ayarlabs.coupahost.com",
            "subject": "Invoice #2735811145 for Amazon Web Services, Inc.",
        })
        assert decision == Decision.ACCEPT
        assert flags.get("is_priority_approval") is True

    def test_approvals_mailbox_wrong_domain_not_flagged(self, monkeypatch):
        # approval_senders only applies within a configured urgent_approvals domain.
        _set_config(monkeypatch, self.CFG)
        _, _, flags = filters.apply_filter({
            "sender_email": "approvals@randomvendor.com",
            "sender_domain": "randomvendor.com",
            "subject": "Invoice #999",
        })
        assert flags.get("is_priority_approval") is not True

    def test_coupa_status_notice_not_flagged(self, monkeypatch):
        # No approval language -> not a priority approval (poll won't pin urgent).
        _set_config(monkeypatch, self.CFG)
        _, _, flags = filters.apply_filter({
            "sender_email": "do_not_reply@coupahost.com",
            "sender_domain": "coupahost.com",
            "subject": "Your purchase order has been received",
        })
        assert flags.get("is_priority_approval") is not True

    def test_blocklist_beats_approval(self, monkeypatch):
        _set_config(monkeypatch, self.CFG)
        monkeypatch.setattr(filters, "_load_blocklist",
                            lambda: {"senders": [{"email": "do_not_reply@coupahost.com"}], "domains": []})
        decision, _, _ = filters.apply_filter({
            "sender_email": "do_not_reply@coupahost.com",
            "sender_domain": "coupahost.com",
            "subject": "Action Required: Approve PO",
        })
        assert decision == Decision.REJECT
