"""Tests for wiki path safety (security-relevant — must not regress)."""
from haven import wiki


class TestIsSafePath:
    def test_index_allowed(self):
        assert wiki.is_safe_path("index.md") is True

    def test_allowed_folder_slug(self):
        assert wiki.is_safe_path("people/lisa-dulchinos.md") is True
        assert wiki.is_safe_path("companies/ayar-labs.md") is True
        assert wiki.is_safe_path("events/2026-q2-audit.md") is True
        assert wiki.is_safe_path("topics/real-estate.md") is True

    def test_schema_and_log_protected(self):
        assert wiki.is_safe_path("SCHEMA.md") is False
        assert wiki.is_safe_path("log.md") is False

    def test_traversal_rejected(self):
        assert wiki.is_safe_path("../secrets.md") is False
        assert wiki.is_safe_path("people/../../etc/passwd.md") is False

    def test_backslash_traversal_rejected(self):
        assert wiki.is_safe_path("people\\..\\..\\x.md") is False

    def test_leading_slash_rejected(self):
        assert wiki.is_safe_path("/etc/passwd.md") is False

    def test_disallowed_folder_rejected(self):
        assert wiki.is_safe_path("secrets/keys.md") is False

    def test_non_md_extension_rejected(self):
        assert wiki.is_safe_path("people/alice.txt") is False

    def test_bare_file_outside_folder_rejected(self):
        assert wiki.is_safe_path("random.md") is False

    def test_uppercase_slug_rejected(self):
        # The slug regex requires lowercase.
        assert wiki.is_safe_path("people/Alice.md") is False
