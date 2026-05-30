"""Tests for the LLM response JSON extraction (pure parser)."""
from haven import llm


class TestExtractJson:
    def test_plain_object(self):
        assert llm._extract_json('{"a": 1}') == '{"a": 1}'

    def test_fenced_json(self):
        raw = '```json\n{"a": 1}\n```'
        assert llm._extract_json(raw) == '{"a": 1}'

    def test_fenced_no_lang(self):
        raw = '```\n{"a": 1}\n```'
        assert llm._extract_json(raw) == '{"a": 1}'

    def test_object_embedded_in_prose(self):
        raw = 'Here you go: {"a": 1} hope that helps'
        assert llm._extract_json(raw) == '{"a": 1}'

    def test_leading_trailing_whitespace(self):
        assert llm._extract_json('   {"a": 1}   ') == '{"a": 1}'

    def test_no_json_returns_text(self):
        assert llm._extract_json("no json here") == "no json here"
