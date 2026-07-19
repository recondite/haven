"""Dispatch — run a draft-producing agent for one item and record the job.

Safe-foundation scope: synchronous "run one agent now" (no queue/breaker/budget
yet — those arrive with real dispatch volume). Agents produce a DRAFT only; they
never send. One agent for now: draft_reply_slack.
"""
from __future__ import annotations

import logging

from haven import config, knowledge, runtime
from haven.db import cursor_store
from haven.spine import spine

log = logging.getLogger("haven")


async def draft_reply_slack(item: dict) -> dict:
    """Draft a Slack reply to a cached Slack message. Returns the draft spec.
    M2: grounded in SecondBrain via context_pack; citations ride in evidence."""
    sender = item.get("sender") or "someone"
    text = item.get("snippet") or item.get("summary") or ""
    pack = knowledge.context_pack(item)
    prompt = (
        "You are drafting a Slack reply that Garth Thompson (CIO, Ayar Labs) will "
        "review before sending. Keep it concise, professional, and direct — lead "
        "with the answer. Do not invent facts you were not given.\n\n"
        f"{knowledge.render_pack(pack)}"
        f"Message from {sender}:\n{text}\n\n"
        "Output ONLY the reply text, no preamble."
    )
    reply = (await runtime.call(prompt, timeout=90)).strip()
    return {
        "kind": "slack",
        "target": item.get("msg_id", ""),   # channel:ts — where the reply would post
        "payload": reply,
        "evidence": [{"source": "slack", "msg_id": item.get("msg_id"), "excerpt": text[:500]}]
                    + pack["citations"],
    }


async def draft_reply_email(item: dict) -> dict:
    """Draft an email reply to a cached Gmail message. Returns the draft spec.
    target = the Gmail msg_id; the executor derives To/Subject/threading from
    Gmail's own headers at send time, never from draft content."""
    sender = item.get("sender") or item.get("from") or "the sender"
    subject = item.get("subject") or ""
    body = item.get("snippet") or item.get("summary") or ""
    pack = knowledge.context_pack(item)
    prompt = (
        "You are drafting an email reply that Garth Thompson (CIO, Ayar Labs) will "
        "review before sending. Style: lead with the answer; bullets over prose; "
        "1-2 lines when possible; professional; sign off exactly 'Thanks, GT'. "
        "Do not invent facts you were not given.\n\n"
        f"{knowledge.render_pack(pack)}"
        f"Email from {sender}\nSubject: {subject}\n\n{body}\n\n"
        "Output ONLY the reply body text, no subject line, no preamble."
    )
    reply = (await runtime.call(prompt, timeout=90)).strip()
    return {
        "kind": "email",
        "target": item.get("msg_id", ""),
        "payload": reply,
        "evidence": [{"source": "gmail", "msg_id": item.get("msg_id"),
                      "excerpt": f"{subject} — {body[:400]}"}]
                    + pack["citations"],
    }


AGENTS = {"draft_reply_slack": draft_reply_slack, "draft_reply_email": draft_reply_email}
DEFAULT_AGENT_BY_SOURCE = {"slack": "draft_reply_slack", "gmail": "draft_reply_email"}


async def run_agent(source: str, msg_id: str, agent_name: str | None = None) -> dict:
    """Load a cached item, run the agent, persist job + draft. No send."""
    agent_name = agent_name or DEFAULT_AGENT_BY_SOURCE.get(source)
    agent = AGENTS.get(agent_name or "")
    if agent is None:
        raise ValueError(f"No dispatch agent for source={source!r} agent={agent_name!r}")

    item = cursor_store.get_cached_payloads(source, [msg_id]).get(msg_id)
    if item is None:
        raise ValueError(f"{source}/{msg_id} not in cache")

    job_id = spine.create_job(agent_name, config.LLM_MODE, f"{source}/{msg_id}")
    spine.log_step(job_id, 1, "load_item", {"source": source, "msg_id": msg_id})
    try:
        spec = await agent(item)
        spine.log_step(job_id, 2, "draft", {"chars": len(spec["payload"])})
        draft_id = spine.create_draft(
            job_id, spec["kind"], spec["target"], spec["payload"], spec.get("evidence")
        )
        spine.finish_job(job_id, "done")
    except Exception as e:
        spine.log_step(job_id, 99, "error", {"error": str(e)[:300]})
        spine.finish_job(job_id, "failed", exit_reason=str(e)[:300])
        raise
    return {"job_id": job_id, "draft_id": draft_id, "preview": spec["payload"][:200]}
