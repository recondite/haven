"""Identity — roster from SecondBrain, system ids resolved from work email.

SecondBrain people pages are the roster source of truth (read-only here). We
parse the "**Title:** / **Department:** / **Manager:** / **Work email:**" lines
into the person table, then resolve each system's id from the work email.
Anything we can't resolve is reported by the unresolved query — never guessed.

ponytail: regex over the known page format, not a markdown parser. Slack is the
only live resolver wired now (its creds exist and it's the plan's example);
jira/freshservice resolvers land with those sources.
"""
from __future__ import annotations

import logging
import re

from haven import config
from haven.db import cursor_store
from haven.spine import spine

log = logging.getLogger("haven")

_PEOPLE_DIR = config.SECONDBRAIN_DIR / "wiki" / "entities" / "people"
_FIELD_RE = {
    "title": re.compile(r"^\*\*Title:\*\*\s*(.+)$", re.M),
    "department": re.compile(r"^\*\*Department:\*\*\s*(.+)$", re.M),
    "manager": re.compile(r"^\*\*Manager:\*\*\s*(.+)$", re.M),
    "work_email": re.compile(r"^\*\*Work email:\*\*\s*(\S+@\S+)", re.M),
}
_NAME_RE = re.compile(r"^#\s+(.+)$", re.M)
_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]\s*")

# Extended bio fields for the person page — parsed on-demand from the SecondBrain
# page body (source of truth), not stored in the person table. Birthday is
# optional (add `**Birthday:**` to a page to populate it; blank until then).
_BIO_RE = {
    "title": re.compile(r"^\*\*Title:\*\*\s*(.+)$", re.M),
    "department": re.compile(r"^\*\*Department:\*\*\s*(.+)$", re.M),
    "division": re.compile(r"^\*\*Division:\*\*\s*(.+)$", re.M),
    "manager": re.compile(r"^\*\*Manager:\*\*\s*(.+)$", re.M),
    "hire_date": re.compile(r"^\*\*Hire date:\*\*\s*(.+)$", re.M),
    "birthday": re.compile(r"^\*\*Birthday:\*\*\s*(.+)$", re.M),
    "location": re.compile(r"^\*\*Location:\*\*\s*(.+)$", re.M),
}


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    return _WIKILINK_RE.sub("", v).strip() or None


_SLUG_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _person_ref(raw: str | None) -> str | None:
    """Manager/person value: prefer trailing display text after the wikilink;
    else humanize the wikilink slug ('[[garth-thompson]]' -> 'Garth Thompson')."""
    if not raw:
        return None
    display = _clean(raw)                     # text with wikilinks stripped
    if display:
        return display
    m = _SLUG_LINK_RE.search(raw)
    if m:
        return m.group(1).replace("-", " ").replace("_", " ").title()
    return None


def bio_fields(page_body: str | None) -> dict:
    """Extract the person-page bio block from a SecondBrain page body. Returns a
    dict with every _BIO_RE key (None when absent). 'team' = division or dept."""
    body = page_body or ""
    raw = {k: (rx.search(body).group(1) if rx.search(body) else None)
           for k, rx in _BIO_RE.items()}
    out = {k: _clean(v) for k, v in raw.items()}
    out["manager"] = _person_ref(raw["manager"])
    # Strip a wrapping parenthetical left by "[[engineering]] (Engineering)".
    for k in ("division", "department"):
        if out[k]:
            out[k] = out[k].strip("()").strip() or out[k]
    out["team"] = out.get("division") or out.get("department")
    return out


def load_roster() -> dict:
    """Parse SecondBrain people pages -> person table. Read-only on SecondBrain.
    Returns {loaded, skipped, gt_reports}."""
    if not _PEOPLE_DIR.is_dir():
        log.warning("SecondBrain people dir not found: %s", _PEOPLE_DIR)
        return {"loaded": 0, "skipped": 0, "gt_reports": 0, "error": "people dir missing"}

    loaded = skipped = reports = 0
    for md in sorted(_PEOPLE_DIR.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        name_m = _NAME_RE.search(text)
        if not name_m:
            skipped += 1
            continue
        fields = {k: _clean(rx.search(text).group(1) if rx.search(text) else None)
                  for k, rx in _FIELD_RE.items()}
        # A direct report of GT = manager line resolves to Garth Thompson.
        is_report = bool(fields["manager"] and "garth" in fields["manager"].lower())
        spine.upsert_person(
            page=md.stem, name=_clean(name_m.group(1)),
            title=fields["title"], department=fields["department"],
            manager=fields["manager"], work_email=fields["work_email"],
            is_report=is_report,
        )
        loaded += 1
        reports += int(is_report)
    log.info("Roster loaded: %d people (%d GT reports)", loaded, reports)
    return {"loaded": loaded, "skipped": skipped, "gt_reports": reports}


async def resolve_slack() -> dict:
    """Resolve Slack user ids for roster people via users.lookupByEmail.
    Best-effort: unresolved people are simply left out (surfaced by the report)."""
    from haven.sources.slack import SlackClient
    people = [p for p in spine.list_people() if p.get("work_email")]
    resolved = 0
    client = SlackClient()
    try:
        for p in people:
            try:
                r = await client._call("users.lookupByEmail", {"email": p["work_email"]}, use_bot=True)
                uid = (r.get("user") or {}).get("id")
                if uid:
                    spine.map_identity(p["id"], "slack", uid, provenance="email_match")
                    resolved += 1
            except Exception as e:  # noqa: BLE001 — expected for non-Slack users
                log.debug("slack lookup miss for %s: %s", p["work_email"], e)
    finally:
        await client.aclose()
    return {"resolved": resolved, "of": len(people)}


async def resolve_jira() -> dict:
    """Resolve Jira accountIds for roster people via user search on work email.
    Best-effort; mirrors resolve_slack. Jira GDPR settings may hide emails, so
    a miss is expected and simply reported, never guessed."""
    if not (config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        return {"resolved": 0, "of": 0, "reason": "jira not configured"}
    from haven.sources.jira import JiraClient
    people = [p for p in spine.list_people() if p.get("work_email")]
    resolved = 0
    client = JiraClient()
    try:
        for p in people:
            try:
                j = await client._call(
                    "GET", "/rest/api/3/user/search", params={"query": p["work_email"]}
                ) or []
                # Prefer an exact email match; else the sole atlassian-account hit.
                acct = None
                for u in j:
                    if (u.get("emailAddress") or "").lower() == p["work_email"].lower():
                        acct = u.get("accountId")
                        break
                if not acct and len(j) == 1 and j[0].get("accountType") == "atlassian":
                    acct = j[0].get("accountId")
                if acct:
                    spine.map_identity(p["id"], "jira", acct, provenance="email_match")
                    resolved += 1
            except Exception as e:  # noqa: BLE001 — expected for non-Jira users
                log.debug("jira lookup miss for %s: %s", p["work_email"], e)
    finally:
        await client.aclose()
    return {"resolved": resolved, "of": len(people)}


async def resolve_asana() -> dict:
    """Resolve Asana user gids for roster people via workspace user search on
    work email. Best-effort; mirrors resolve_slack/resolve_jira."""
    if not config.ASANA_TOKEN:
        return {"resolved": 0, "of": 0, "reason": "asana not configured"}
    from haven.sources.asana import AsanaClient
    people = [p for p in spine.list_people() if p.get("work_email")]
    resolved = 0
    client = AsanaClient()
    try:
        me = await client.me()
        ws = me["workspace_gid"]
        if not ws:
            return {"resolved": 0, "of": len(people), "reason": "no workspace"}
        # One workspace-users fetch, then match by email locally.
        j = await client._call(
            "GET", f"/workspaces/{ws}/users", params={"opt_fields": "email,name"}
        ) or {}
        by_email = {
            (u.get("email") or "").lower(): str(u.get("gid"))
            for u in (j.get("data") or []) if u.get("email")
        }
        for p in people:
            gid = by_email.get(p["work_email"].lower())
            if gid:
                spine.map_identity(p["id"], "asana", gid, provenance="email_match")
                resolved += 1
    except Exception as e:  # noqa: BLE001
        log.debug("asana resolve failed: %s", e)
    finally:
        await client.aclose()
    return {"resolved": resolved, "of": len(people)}


def _item_matches_person(src: str, it: dict, email: str,
                         slack_ids: set[str], jira_ids: set[str],
                         asana_ids: set[str] | None = None) -> bool:
    """Per-source attribution — the fix for the email-only rollup that missed
    Slack (blank sender_email) and Jira (accountId, not email)."""
    if src in ("gmail", "freshservice"):
        m = _EMAIL_RE.search(it.get("sender_email") or it.get("sender") or it.get("from") or "")
        return bool(email) and bool(m) and m.group(0).lower() == email
    if src == "slack":
        return bool(slack_ids) and it.get("sender_id") in slack_ids
    if src == "jira":
        return bool(jira_ids) and (
            it.get("assignee_account_id") in jira_ids
            or it.get("reporter_account_id") in jira_ids
        )
    if src == "asana":
        return bool(asana_ids) and it.get("assignee_gid") in asana_ids
    if src == "otter":
        guests = {(g or "").lower() for g in (it.get("calendar_guest_emails") or [])}
        return bool(email) and email in guests
    return False


def items_for_person(person: dict, identities: list[dict]) -> dict:
    """All cached items attributable to this person, per source, split
    open/handled. Newest first. UI-ready rows only (no raw payloads)."""
    email = (person.get("work_email") or "").lower()
    slack_ids = {i["system_id"] for i in identities if i["system"] == "slack"}
    jira_ids = {i["system_id"] for i in identities if i["system"] == "jira"}
    asana_ids = {i["system_id"] for i in identities if i["system"] == "asana"}
    buckets: dict[str, dict[str, list]] = {
        src: {"open": [], "handled": []} for src in config.KNOWN_SOURCES
    }
    for src in config.KNOWN_SOURCES:
        for it in cursor_store.list_cached(src):
            if not _item_matches_person(src, it, email, slack_ids, jira_ids, asana_ids):
                continue
            row = {
                "msg_id": it.get("msg_id"), "source": src,
                "subject": it.get("subject") or it.get("snippet") or "",
                "tag": it.get("tag"), "urgency": it.get("urgency"),
                "date": it.get("date"), "deeplink": it.get("deeplink"),
                "handled_at": it.get("handled_at"),
            }
            buckets[src]["handled" if it.get("handled_at") else "open"].append(row)
    for src in buckets:
        for k in ("open", "handled"):
            buckets[src][k].sort(key=lambda r: r.get("date") or "", reverse=True)
    return buckets


def unresolved_senders(limit: int = 200) -> list[dict]:
    """Senders seen in cached items whose email doesn't map to any person.
    Derived, not stored — the 'never silently wrong' queue."""
    emails: dict[str, dict] = {}
    for src in config.KNOWN_SOURCES:
        for it in cursor_store.list_cached(src):
            email = _extract_email(it.get("sender") or it.get("from") or "")
            if email and email not in emails:
                emails[email] = {"email": email, "source": src,
                                 "sample": (it.get("subject") or it.get("snippet") or "")[:80]}
    out = [v for k, v in emails.items() if spine.person_by_email(k) is None]
    return out[:limit]


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_email(s: str) -> str | None:
    m = _EMAIL_RE.search(s or "")
    return m.group(0).lower() if m else None


async def scheduled_drift() -> dict:
    """Weekly task: refresh roster + Slack ids, then report drift. Logs the
    proposal; writes nothing (changes go through approval-gated ingest)."""
    load_roster()
    try:
        await resolve_slack()
    except Exception as e:  # noqa: BLE001
        log.warning("drift: slack resolve failed: %s", e)
    try:
        await resolve_jira()
    except Exception as e:  # noqa: BLE001
        log.warning("drift: jira resolve failed: %s", e)
    try:
        await resolve_asana()
    except Exception as e:  # noqa: BLE001
        log.warning("drift: asana resolve failed: %s", e)
    report = roster_drift()
    log.info("Roster drift: %d candidate joiners, %d without slack id",
             len(report["candidate_joiners"]), len(report["roster_people_without_slack_id"]))
    return report


def roster_drift() -> dict:
    """Weekly drift check: diff live signals against the SecondBrain roster and
    PROPOSE (never write) page updates. Right-sized per plan v4 §Phase 3 — a
    report, not a reconciliation engine. Approval-gated changes go through ingest.

    Two signals available without JIRA:
      - candidate_joiners: @ayarlabs.com senders in live items with no roster page
      - candidate_leavers/stale: roster people whose Slack id no longer resolves
    """
    roster_emails = {p["work_email"].lower() for p in spine.list_people()
                     if p.get("work_email")}
    # Joiners: internal senders we're seeing but don't have a page for.
    joiners: dict[str, str] = {}
    for src in config.KNOWN_SOURCES:
        for it in cursor_store.list_cached(src):
            email = _extract_email(it.get("sender") or it.get("from") or "")
            if email and email.endswith("@ayarlabs.com") and email not in roster_emails:
                joiners.setdefault(email, it.get("sender") or email)
    # Stale: roster people with an email but no resolved Slack id (departed, or
    # email drift). Only a signal — humans decide.
    stale = []
    for p in spine.list_people():
        if not p.get("work_email"):
            continue
        has_slack = any(i["system"] == "slack" for i in spine.identities_for_person(p["id"]))
        if not has_slack:
            stale.append({"name": p["name"], "email": p["work_email"], "page": p["secondbrain_page"]})
    return {
        "candidate_joiners": [{"email": e, "seen_as": n} for e, n in sorted(joiners.items())],
        "roster_people_without_slack_id": stale,
        "proposal": "Review joiners for new SecondBrain pages (via ingest); "
                    "verify stale entries aren't departures. No pages were changed.",
    }
