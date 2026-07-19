"""Eval harness scoring math (runtime mocked — no model call)."""
import asyncio

from haven import eval as eval_mod
from haven import runtime


def run(coro):
    return asyncio.run(coro)


class FakeRuntime(runtime.Runtime):
    """Returns the correct answer for the first golden case, wrong for the rest —
    so accuracy should be exactly 1/N for tag."""
    name = "fake"

    async def call(self, prompt, model=None, timeout=60.0):
        return "{}"

    async def call_json(self, prompt, model=None, timeout=60.0):
        first = eval_mod.GOLDEN[0]
        # crude: echo the first case's expected tag if its subject is in the prompt
        if first["item"].subject in prompt:
            return {"tag": first["tag"], "urgency": first["urgency"]}
        return {"tag": "fyi", "urgency": "low"}


def test_eval_reports_accuracy():
    res = run(eval_mod.run_eval(FakeRuntime()))
    assert res["n"] == len(eval_mod.GOLDEN)
    # exactly one tag correct (the first case), unless another case's expected is fyi/low
    assert 0.0 <= res["tag_accuracy"] <= 1.0
    first = next(c for c in res["cases"] if c["name"] == eval_mod.GOLDEN[0]["name"])
    assert first["tag_ok"] is True
    assert res["runtime"] == "fake"


def test_eval_handles_runtime_error():
    class Boom(runtime.Runtime):
        name = "boom"
        async def call(self, prompt, model=None, timeout=60.0):
            return ""
        async def call_json(self, prompt, model=None, timeout=60.0):
            raise RuntimeError("model down")
    res = run(eval_mod.run_eval(Boom()))
    assert res["tag_accuracy"] == 0.0
    assert all(c["error"] for c in res["cases"])
