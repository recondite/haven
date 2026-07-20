"""Spine migration validation. Compares the dual-written `item` table against the
authoritative `cached_items` cache so the read-path flip can wait for parity."""
from fastapi import APIRouter, HTTPException

from haven import config
from haven.db import cursor_store
from haven.spine import spine

router = APIRouter(prefix="/api/spine", tags=["spine"])


@router.get("/diff/{source}")
async def diff(source: str) -> dict:
    if source not in config.KNOWN_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    cached = {p["msg_id"]: p for p in cursor_store.list_cached(source) if p.get("msg_id")}
    mismatches = spine.diff_source(cached, source)
    return {"source": source, "cached": len(cached), "mismatches": mismatches, "clean": not mismatches}
