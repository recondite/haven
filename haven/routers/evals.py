"""Golden-set eval endpoint. On-demand — a run hits the active model N times."""
from fastapi import APIRouter, HTTPException

from haven import eval as eval_mod
from haven.config import KNOWN_SOURCES
from haven.db import cursor_store

router = APIRouter(prefix="/api/eval", tags=["eval"])


@router.get("/scoring")
@router.post("/scoring")
async def scoring_eval() -> dict:
    return await eval_mod.run_eval()


@router.get("/retrieval")
async def retrieval() -> dict:
    return eval_mod.retrieval_eval()


@router.post("/promote")
async def promote(payload: dict) -> dict:
    """Promote a cached Gmail item into the scoring golden set as a confirmed
    case. Body: {source, msg_id, tag, urgency}. Gmail only (email prompt)."""
    source = (payload.get("source") or "").strip()
    msg_id = (payload.get("msg_id") or "").strip()
    if source != "gmail":
        raise HTTPException(400, "eval promotion supports gmail items only for now")
    if source not in KNOWN_SOURCES:
        raise HTTPException(400, f"unknown source {source}")
    item = cursor_store.get_cached_payloads(source, [msg_id]).get(msg_id)
    if not item:
        raise HTTPException(404, f"{source}/{msg_id} not in cache")
    fields = {
        "sender_name": item.get("sender_name") or item.get("sender") or "",
        "sender_email": item.get("sender_email") or "",
        "sender_company": item.get("sender_company") or "",
        "sender_domain": item.get("sender_domain") or "",
        "subject": item.get("subject") or "",
        "body_text": item.get("body_text") or item.get("snippet") or "",
        "date": item.get("date") or "",
        "garth_recipient_role": item.get("garth_recipient_role") or "to",
    }
    total = eval_mod.promote_case(
        fields, payload.get("tag") or item.get("tag") or "fyi",
        payload.get("urgency") or item.get("urgency") or "low",
        name=f"promoted-{msg_id[:16]}")
    return {"promoted": True, "total_promoted": total}


@router.post("/distill-style")
async def distill_style() -> dict:
    return await eval_mod.distill_style()
