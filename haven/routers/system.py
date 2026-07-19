"""System control endpoints (M0): send-mode state + panic switch, stuck-send
resolution, backup status. All mutations audited."""
import logging

from fastapi import APIRouter, HTTPException

from haven import backup, config, executor, knowledge
from haven.spine import spine

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
    }


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
