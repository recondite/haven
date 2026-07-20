"""Dispatch + approval queue endpoints. Approve is DRY-RUN (no external send)."""
import json
import logging

from fastapi import APIRouter, HTTPException

from haven import dispatch, executor
from haven.spine import spine

log = logging.getLogger("haven")

router = APIRouter(prefix="/api", tags=["dispatch"])


@router.post("/dispatch/run")
async def dispatch_run(payload: dict) -> dict:
    """Run a draft-producing agent for one item. Body: {source, msg_id, agent?}."""
    source = (payload.get("source") or "").strip()
    msg_id = (payload.get("msg_id") or "").strip()
    if not source or not msg_id:
        raise HTTPException(400, "source and msg_id required")
    try:
        return await dispatch.run_agent(source, msg_id, payload.get("agent"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("dispatch run failed: %s", e)
        raise HTTPException(500, f"dispatch failed: {type(e).__name__}: {e}")


@router.get("/approvals")
async def list_approvals() -> dict:
    drafts = spine.list_drafts("pending")
    for d in drafts:
        if d.get("evidence"):
            try:
                d["evidence"] = json.loads(d["evidence"])
            except Exception:
                pass
    return {"drafts": drafts, "dry_run": executor.is_dry_run()}


@router.post("/approvals/{draft_id}/approve")
async def approve(draft_id: int) -> dict:
    try:
        return await executor.approve(draft_id)
    except executor.ExecutorError as e:
        raise HTTPException(400, str(e))


@router.post("/approvals/{draft_id}/edit")
async def edit(draft_id: int, payload: dict) -> dict:
    """Edit a pending draft's text before approving. Body: {"payload": "..."}."""
    try:
        return executor.edit(draft_id, payload.get("payload", ""))
    except executor.ExecutorError as e:
        raise HTTPException(400, str(e))


@router.post("/approvals/{draft_id}/reject")
async def reject(draft_id: int, payload: dict | None = None) -> dict:
    try:
        return executor.reject(draft_id, (payload or {}).get("reason", ""))
    except executor.ExecutorError as e:
        raise HTTPException(400, str(e))
