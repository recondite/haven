"""Golden-set eval endpoint. On-demand — a run hits the active model N times."""
from fastapi import APIRouter

from haven import eval as eval_mod

router = APIRouter(prefix="/api/eval", tags=["eval"])


@router.get("/scoring")
@router.post("/scoring")
async def scoring_eval() -> dict:
    return await eval_mod.run_eval()
