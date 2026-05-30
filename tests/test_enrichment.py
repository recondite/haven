"""Tests for the deterministic enrichment helpers (pure functions, no I/O)."""
from haven import enrichment


class TestCompanyFromDomain:
    def test_known_domain(self):
        assert enrichment.company_from_domain("ayarlabs.com") == "Ayar Labs"

    def test_known_subdomain_walks_up(self):
        # email.claude.com isn't known, but its parent isn't either — falls to root label.
        assert enrichment.company_from_domain("mail.google.com") == "Google"

    def test_unknown_uses_registrable_root_not_subdomain(self):
        # The bug this guards: "email.claude.com" must become "Claude", not "Email".
        assert enrichment.company_from_domain("email.claude.com") == "Claude"

    def test_hyphen_and_underscore_titleized(self):
        assert enrichment.company_from_domain("foo-bar.io") == "Foo Bar"

    def test_empty(self):
        assert enrichment.company_from_domain("") == ""


class TestGarthRecipientRole:
    TO = [{"email": "garth@ayarlabs.com", "name": "Garth"}]
    CC = [{"email": "garth@ayarlabs.com", "name": "Garth"}]

    def test_to(self):
        assert enrichment.garth_recipient_role("garth@ayarlabs.com", self.TO, []) == "to"

    def test_cc(self):
        assert enrichment.garth_recipient_role("garth@ayarlabs.com", [], self.CC) == "cc"

    def test_bcc_when_neither(self):
        assert enrichment.garth_recipient_role("garth@ayarlabs.com", [], []) == "bcc"

    def test_case_insensitive(self):
        to = [{"email": "Garth@AyarLabs.com"}]
        assert enrichment.garth_recipient_role("garth@ayarlabs.com", to, []) == "to"

    def test_empty_user(self):
        assert enrichment.garth_recipient_role("", self.TO, []) == ""


class TestDatesMentioned:
    def test_iso_and_relative(self):
        out = enrichment.dates_mentioned("Let's meet 2026-06-01 or tomorrow.")
        assert "2026-06-01" in out
        assert "tomorrow" in out

    def test_dedup_and_cap(self):
        text = " ".join(["today"] * 20)
        out = enrichment.dates_mentioned(text)
        assert out == ["today"]  # deduped

    def test_empty(self):
        assert enrichment.dates_mentioned("") == []


class TestDeriveThreadState:
    def _msg(self, frm, ts):
        return {
            "internalDate": str(ts),
            "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Date", "value": f"d{ts}"}]},
        }

    def test_garth_owns_when_he_replied_last(self):
        msgs = [
            self._msg("someone@x.com", 1000),
            self._msg("garth@ayarlabs.com", 2000),
        ]
        st = enrichment.derive_thread_state(msgs, "garth@ayarlabs.com")
        assert st["garth_owns_last_turn"] is True
        assert st["thread_message_count"] == 2
        assert st["last_outbound_at"] == "d2000"
        assert st["last_inbound_at"] == "d1000"

    def test_other_owns_when_they_replied_last(self):
        msgs = [
            self._msg("garth@ayarlabs.com", 1000),
            self._msg("someone@x.com", 2000),
        ]
        st = enrichment.derive_thread_state(msgs, "garth@ayarlabs.com")
        assert st["garth_owns_last_turn"] is False

    def test_only_inbound(self):
        msgs = [self._msg("someone@x.com", 1000)]
        st = enrichment.derive_thread_state(msgs, "garth@ayarlabs.com")
        assert st["garth_owns_last_turn"] is False
        assert st["last_outbound_at"] is None

    def test_empty_thread(self):
        st = enrichment.derive_thread_state([], "garth@ayarlabs.com")
        assert st["thread_message_count"] == 0
        assert st["garth_owns_last_turn"] is False
