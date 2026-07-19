"""System control endpoints (M0): send-mode state + panic switch, stuck-send
resolution, backup status. All mutations audited."""
import logging

from fastapi import APIRouter, HTTPException

from haven import backup, config, executor, knowledge
from haven.db import cursor_store
from haven.spine import spine

# Write capabilities each connected token holds — the trust surface. Executor
# verbs (slack/email/wiki) get live 30-day use counts; the rest are capability
# statements (archive/linear aren't in the action ledger).
_WRITE_VERBS = [
    {"connection": "Gmail", "verb": "send reply", "kind": "email", "risk": "high"},
    {"connection": "Gmail", "verb": "archive (label)", "kind": None, "risk": "low"},
    {"connection": "Slack", "verb": "post to thread", "kind": "slack", "risk": "high"},
    {"connection": "Linear", "verb": "create / close issue", "kind": None, "risk": "med"},
    {"connection": "SecondBrain", "verb": "create page", "kind": "wiki", "risk": "med"},
]
_ROUTINES = [
    ("gmail", "Gmail poll"), ("slack", "Slack poll"),
    ("freshservice", "Freshservice poll"), ("otter", "Otter poll"),
]

log = logging.getLogger("haven")

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/state")
async def state() -> dict:
    override = spine.get_runtime_config("send_mode")
    return {
        "send_mode": {
            "effective": "dry" if executor.is_dry_run() else "live",
            "env_default": config.SEND_MODE,
            "override": override,
            "source": "override" if override in ("dry", "live") else "env",
            "forced_reason": spine.get_runtime_config("send_mode_forced_reason"),
        },
        "stuck_actions": spine.list_actions("sending"),
        "backups": backup.backup_status(),
        "curation": knowledge.curation_backlog(),
        "trust": _trust_panel(),
    }


def _trust_panel() -> dict:
    """M4: write-verb usage (unused-flagged), routine staleness, agent feedback."""
    counts = spine.action_verb_counts(30)
    verbs = []
    for v in _WRITE_VERBS:
        c = counts.get(v["kind"], {}) if v["kind"] else {}
        tracked = v["kind"] is not None
        verbs.append({**v, "count_30d": c.get("count") if tracked else None,
                      "last_used": c.get("last_used") if tracked else None,
                      "unused": tracked and not c.get("count"),
                      "tracked": tracked})
    routines = [{"name": label, "last_poll": cursor_store.get_cursor(src, "last_poll")}
                for src, label in _ROUTINES]
    return {"write_verbs": verbs, "routines": routines,
            "agent_feedback": spine.feedback_by_agent()}


@router.post("/send-mode")
async def send_mode(payload: dict) -> dict:
    try:
        return executor.set_send_mode((payload.get("mode") or "").strip())
    except executor.ExecutorError as e:
        raise HTTPException(400, str(e))


@router.post("/actions/{action_id}/resolve")
async def resolve(action_id: int, payload: dict) -> dict:
    try:
        return spine.resolve_action(
            action_id, (payload.get("status") or "").strip(), payload.get("note") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/backup")
async def backup_now() -> dict:
    return {"results": backup.backup_now()}
