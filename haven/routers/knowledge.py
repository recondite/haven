"""SecondBrain knowledge endpoints: search (cited) + page fetch."""
from fastapi import APIRouter, HTTPException

from haven import executor, knowledge
from haven.spine import spine

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/search")
async def search(q: str, limit: int = 8) -> dict:
    hits = knowledge.search(q, limit=limit)
    return {"query": q, "count": len(hits), "hits": hits,
            "note": "SecondBrain first — cite the path field for any answer drawn from a hit."}


@router.get("/page")
async def page(path: str) -> dict:
    content = knowledge.get_page(path)
    if content is None:
        raise HTTPException(404, f"page not found or outside SecondBrain: {path}")
    return {"path": path, "content": content}


@router.post("/ingest")
async def ingest(payload: dict) -> dict:
    """Draft a new SecondBrain page into the approval queue (no write until GT
    approves). Body: {title, type, tags[], body}. Validated up-front so a
    schema-invalid or duplicate ingest is rejected before it becomes a draft."""
    title = (payload.get("title") or "").strip()
    type_ = (payload.get("type") or "").strip()
    if not title or not type_:
        raise HTTPException(400, "title and type required")
    target = knowledge.ingest_target(title, type_)
    page_md = knowledge.build_page(title, type_, payload.get("tags") or [], payload.get("body") or "")
    try:
        executor.validate_wiki(page_md, target)  # same gate approve() enforces
    except executor.ExecutorError as e:
        raise HTTPException(400, f"schema/validation: {e}")
    draft_id = spine.create_draft(None, "wiki", target, page_md,
                                  evidence=[{"source": "ingest", "note": payload.get("source", "manual")}])
    return {"draft_id": draft_id, "target": target, "status": "pending",
            "note": "Review in Approvals; approving writes the page to SecondBrain (live mode)."}
