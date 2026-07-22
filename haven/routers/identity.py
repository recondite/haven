"""Identity + People endpoints: roster load, id resolution, coverage, dossier."""
import logging
import urllib.parse

from fastapi import APIRouter, Body, HTTPException

from haven import config, identity, knowledge
from haven.spine import spine

log = logging.getLogger("haven")

router = APIRouter(prefix="/api", tags=["identity"])


@router.post("/identity/load-roster")
async def load_roster() -> dict:
    return identity.load_roster()


@router.post("/identity/resolve/slack")
async def resolve_slack() -> dict:
    return await identity.resolve_slack()


@router.post("/identity/resolve/jira")
async def resolve_jira() -> dict:
    return await identity.resolve_jira()


@router.post("/identity/resolve/asana")
async def resolve_asana() -> dict:
    return await identity.resolve_asana()


@router.get("/identity/coverage")
async def coverage() -> dict:
    cov = spine.identity_coverage()
    cov["unresolved_senders"] = len(identity.unresolved_senders())
    return cov


@router.get("/identity/unresolved")
async def unresolved() -> dict:
    return {"unresolved": identity.unresolved_senders()}


@router.get("/identity/drift")
async def drift() -> dict:
    """Weekly roster-drift report (proposes, never writes)."""
    return identity.roster_drift()


@router.get("/people")
async def people(reports_only: bool = False) -> dict:
    return {"people": spine.list_people(reports_only=reports_only)}


def _deeplinks(person: dict, identities: list[dict]) -> dict:
    """Best-effort outbound links. Only include what we can build honestly."""
    links: dict[str, str] = {}
    email = person.get("work_email") or ""
    if email:
        q = urllib.parse.quote(f"from:{email} OR to:{email}")
        links["gmail"] = f"https://mail.google.com/mail/u/0/#search/{q}"
    jira_id = next((i["system_id"] for i in identities if i["system"] == "jira"), None)
    if jira_id and config.JIRA_BASE_URL:
        jql = urllib.parse.quote(f'assignee = "{jira_id}" OR reporter = "{jira_id}" ORDER BY updated DESC')
        links["jira"] = f"{config.JIRA_BASE_URL.rstrip('/')}/issues/?jql={jql}"
    slack_id = next((i["system_id"] for i in identities if i["system"] == "slack"), None)
    if slack_id:
        links["slack"] = f"https://slack.com/app_redirect?channel={slack_id}"
    return links


@router.get("/people/{person_id}/rollup")
async def rollup(person_id: int) -> dict:
    """Person dossier: everything Haven can attribute to this person across
    sources (open + handled), their SecondBrain page, Garth's pinned notes, and
    outbound deeplinks. Aggregated on-demand from caches — never staler than the
    last poll, nothing stored except the notes."""
    people = {p["id"]: p for p in spine.list_people()}
    person = people.get(person_id)
    if not person:
        raise HTTPException(404, f"person {person_id} not found")

    identities = spine.identities_for_person(person_id)
    buckets = identity.items_for_person(person, identities)
    open_counts = {k: len(v["open"]) for k, v in buckets.items()}
    last_touch = {}
    for src, b in buckets.items():
        dates = [r["date"] for r in (b["open"] + b["handled"]) if r.get("date")]
        if dates:
            last_touch[src] = max(dates)

    sb_page = person.get("secondbrain_page")
    sb_body = knowledge.get_page(f"wiki/entities/people/{sb_page}.md") if sb_page else None

    # Bio block from the SecondBrain page, with the person record as fallback.
    bio = identity.bio_fields(sb_body)
    bio["name"] = person.get("name")
    bio["title"] = bio.get("title") or person.get("title")
    bio["manager"] = bio.get("manager") or person.get("manager")
    bio["team"] = bio.get("team") or person.get("department")

    return {
        "person": person,
        "bio": bio,                             # name/birthday/hire_date/title/manager/team
        "identities": identities,
        "items": buckets,                       # per-source {open:[], handled:[]}
        "open_counts": open_counts,
        "open_total": sum(open_counts.values()),
        "last_touch": last_touch,
        "deeplinks": _deeplinks(person, identities),
        "secondbrain": {"page": sb_page, "body": sb_body} if sb_page else None,
        "notes": spine.list_notes(person_id),
    }


@router.get("/people/{person_id}/notes")
async def list_notes(person_id: int) -> dict:
    return {"notes": spine.list_notes(person_id)}


@router.post("/people/{person_id}/notes")
async def add_note(person_id: int, body: str = Body(..., embed=True)) -> dict:
    if not spine.list_people() or person_id not in {p["id"] for p in spine.list_people()}:
        raise HTTPException(404, f"person {person_id} not found")
    try:
        return spine.add_note(person_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/people/notes/{note_id}/hide")
async def hide_note(note_id: int) -> dict:
    return {"hidden": spine.hide_note(note_id)}
