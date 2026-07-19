"""Identity + People endpoints: roster load, id resolution, coverage, rollup."""
import logging
import re

from fastapi import APIRouter, HTTPException

from haven import config, identity
from haven.db import cursor_store
from haven.spine import spine

log = logging.getLogger("haven")

router = APIRouter(prefix="/api", tags=["identity"])

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@router.post("/identity/load-roster")
async def load_roster() -> dict:
    return identity.load_roster()


@router.post("/identity/resolve/slack")
async def resolve_slack() -> dict:
    return await identity.resolve_slack()


@router.get("/identity/coverage")
async def coverage() -> dict:
    cov = spine.identity_coverage()
    cov["unresolved_senders"] = len(identity.unresolved_senders())
    return cov


@router.get("/identity/unresolved")
async def unresolved() -> dict:
    return {"unresolved": identity.unresolved_senders()}


@router.get("/people")
async def people(reports_only: bool = False) -> dict:
    return {"people": spine.list_people(reports_only=reports_only)}


@router.get("/people/{person_id}/rollup")
async def rollup(person_id: int) -> dict:
    people = {p["id"]: p for p in spine.list_people()}
    person = people.get(person_id)
    if not person:
        raise HTTPException(404, f"person {person_id} not found")
    email = (person.get("work_email") or "").lower()

    # Items across sources attributable to this person by sender email. Honest
    # subset: JIRA + a dedicated request queue arrive with Phase 2's later steps.
    buckets: dict[str, list] = {"freshservice": [], "otter": [], "gmail": [], "slack": []}
    for src in config.KNOWN_SOURCES:
        for it in cursor_store.list_cached(src):
            if it.get("handled_at"):
                continue
            m = _EMAIL_RE.search(it.get("sender") or it.get("from") or "")
            if m and m.group(0).lower() == email and email:
                buckets[src].append({
                    "msg_id": it.get("msg_id"), "subject": it.get("subject") or it.get("snippet"),
                    "tag": it.get("tag"), "urgency": it.get("urgency"),
                })
    return {
        "person": person,
        "identities": spine.identities_for_person(person_id),
        "open_items": buckets,
        "counts": {k: len(v) for k, v in buckets.items()},
        "pending": ["jira (source not yet wired)", "requests-to-GT (request queue, Phase 2)"],
    }
