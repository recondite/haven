"""LLM runtime seam — one interface, swappable backend.

Generalizes llm.py (Claude CLI) so the same scoring/wiki calls can route to a
local model by config, with no call-site change. Selection = HAVEN_LLM_MODE.

ponytail: single module + two real implementations (an interface with one impl
would be dead weight; two isn't). Promote to a package when Phase 1 adds the
agent-dispatch `run(spec, ctx) -> {drafts, ...}` shape and Hermes/Codex runtimes.

Live local verification needs Ollama (or LM Studio) actually running — the tests
here use a mock transport, which proves the seam, not a specific model.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging

import httpx

from haven import config, llm

log = logging.getLogger("haven")


class Runtime(abc.ABC):
    """Text-in/text-out LLM backend. `call_json` is shared: it wraps `call` with
    fence-stripping and a one-shot retry (the empty/garbled first response is the
    common transient failure across backends)."""

    name: str

    @abc.abstractmethod
    async def call(self, prompt: str, model: str | None = None, timeout: float = 60.0) -> str:
        ...

    async def call_json(self, prompt: str, model: str | None = None, timeout: float = 60.0) -> dict:
        last_error: Exception | None = None
        last_raw = ""
        for attempt in (1, 2):
            raw = await self.call(prompt, model, timeout)
            last_raw = raw
            cleaned = llm._extract_json(raw)
            if not cleaned.strip():
                last_error = ValueError("empty output")
                log.warning("[%s] call_json attempt %d: empty output", self.name, attempt)
            else:
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError as e:
                    last_error = e
                    log.warning("[%s] call_json attempt %d: parse failed (%s) raw[:200]=%r",
                                self.name, attempt, e, raw[:200])
            if attempt == 1:
                await asyncio.sleep(0.5)
        raise RuntimeError(f"[{self.name}] call_json failed after 2 attempts: "
                           f"{last_error}; raw[:200]={last_raw[:200]!r}")


class ClaudeRuntime(Runtime):
    """Claude CLI shell-out. Reuses llm.py's Windows-hardened resolver as-is."""
    name = "claude"

    async def call(self, prompt: str, model: str | None = None, timeout: float = 60.0) -> str:
        return await llm.claude_call(prompt, model, timeout)


class LocalRuntime(Runtime):
    """OpenAI-compatible local endpoint (Ollama / LM Studio).

    The `model` arg carries Claude tier hints from callers and is meaningless
    here, so it's ignored — every call uses the single configured local model.
    """
    name = "local"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = (base_url or config.LOCAL_LLM_BASE_URL).rstrip("/")
        self.model = model or config.LOCAL_LLM_MODEL
        self._client = client  # injectable for tests

    async def call(self, prompt: str, model: str | None = None, timeout: float = 60.0) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "stream": False,
        }
        if self._client is not None:
            resp = await self._client.post("/chat/completions", json=payload, timeout=timeout)
        else:
            async with httpx.AsyncClient(base_url=self.base_url) as c:
                resp = await c.post("/chat/completions", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


_INSTANCES: dict[str, Runtime] = {}


def get_runtime(name: str | None = None) -> Runtime:
    """Return the configured (or named) runtime, cached per name."""
    name = (name or config.LLM_MODE or "cli").lower()
    key = "claude" if name in ("cli", "claude") else name
    if key not in _INSTANCES:
        if key == "local":
            _INSTANCES[key] = LocalRuntime()
        elif key == "claude":
            _INSTANCES[key] = ClaudeRuntime()
        else:
            raise ValueError(f"Unknown LLM runtime: {name!r}")
    return _INSTANCES[key]


# Module-level convenience so call sites read `runtime.call_json(...)`.
async def call(prompt: str, model: str | None = None, timeout: float = 60.0) -> str:
    return await get_runtime().call(prompt, model, timeout)


async def call_json(prompt: str, model: str | None = None, timeout: float = 60.0) -> dict:
    return await get_runtime().call_json(prompt, model, timeout)
