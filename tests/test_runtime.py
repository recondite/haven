"""Runtime seam: both backends satisfy call/call_json offline; factory selects
by config. Live Ollama is out of scope here — MockTransport proves the seam.

No pytest-asyncio in this repo, so coroutines are driven with asyncio.run."""
import asyncio

import httpx
import pytest

from haven import config, llm, runtime


def run(coro):
    return asyncio.run(coro)


def _local_with_reply(content: str) -> runtime.LocalRuntime:
    """A LocalRuntime whose endpoint always returns `content` as the completion."""
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x/v1")
    return runtime.LocalRuntime(client=client)


def test_local_call_returns_content():
    r = _local_with_reply("hello from local")
    assert run(r.call("hi")) == "hello from local"


def test_local_call_json_parses_fenced():
    r = _local_with_reply('```json\n{"tag": "fyi"}\n```')
    assert run(r.call_json("hi")) == {"tag": "fyi"}


def test_claude_runtime_uses_llm(monkeypatch):
    async def fake_call(prompt, model=None, timeout=60.0):
        return '{"urgency": "high"}'
    monkeypatch.setattr(llm, "claude_call", fake_call)
    assert run(runtime.ClaudeRuntime().call_json("x")) == {"urgency": "high"}


def test_call_json_retries_then_succeeds():
    """First response empty, second valid — shared retry recovers it."""
    calls = {"n": 0}

    class Flaky(runtime.Runtime):
        name = "flaky"
        async def call(self, prompt, model=None, timeout=60.0):
            calls["n"] += 1
            return "" if calls["n"] == 1 else '{"ok": true}'

    assert run(Flaky().call_json("x")) == {"ok": True}
    assert calls["n"] == 2


def test_call_json_gives_up_after_two():
    class Bad(runtime.Runtime):
        name = "bad"
        async def call(self, prompt, model=None, timeout=60.0):
            return "not json at all"
    with pytest.raises(RuntimeError):
        run(Bad().call_json("x"))


def test_factory_selects_by_config(monkeypatch):
    runtime._INSTANCES.clear()
    monkeypatch.setattr(config, "LLM_MODE", "cli")
    assert isinstance(runtime.get_runtime(), runtime.ClaudeRuntime)
    runtime._INSTANCES.clear()
    monkeypatch.setattr(config, "LLM_MODE", "local")
    assert isinstance(runtime.get_runtime(), runtime.LocalRuntime)
    runtime._INSTANCES.clear()
