"""Phase 1.9 — cross-source contact derivation.

Pure function: takes every cached item across Gmail / Slack / Freshservice /
Otter, returns a deduped list of contacts with aggregated counts. No DB writes,
no LLM calls — recomputed from scratch on each request. Cheap because it's
deterministic and the data is already in memory.

Per-source contributions:
  - gmail:        sender_email/name/company; "owes reply" signal via
                  garth_owns_last_turn / last_inbound_at.
  - slack:        sender_email/name (often empty for channel messages).
  - freshservice: requester_email/name (sender_email/sender_name on the payload).
  - otter:        calendar_guest_emails (excluding the assignee — Garth — since
                  Otter ARs are always assigned to him).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


def _lc_email(s: str | None) -> str:
    return (s or "").strip().lower()


def _domain_of(email: str) -> str:
    return email.split("@", 1)[1] if "@" in email else ""


@dataclass
class Contact:
    email: str
    name: str = ""
    company: str = ""
    domain: str = ""
    first_seen: str = ""                       # ISO of earliest item.date
    last_seen: str = ""                        # ISO of latest item.date
    sources: set[str] = field(default_factory=set)
    counts_by_source: dict[str, int] = field(default_factory=dict)
    open_count: int = 0                        # not handled, not captured to Linear
    handled_count: int = 0                     # marked done OR captured to Linear
    owes_reply_count: int = 0                  # gmail-only: garth doesn't own last turn
    item_msg_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "company": self.company,
            "domain": self.domain,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sources": sorted(self.sources),
            "counts_by_source": self.counts_by_source,
            "open_count": self.open_count,
            "handled_count": self.handled_count,
            "owes_reply_count": self.owes_reply_count,
            "item_msg_ids": self.item_msg_ids,
        }


def _is_handled(item: dict) -> bool:
    """Mirror the frontend's isHandled — captured to Linear OR explicit Mark-done."""
    return bool(item.get("handled_at")) or bool(item.get("linear_id"))


def _record(
    contacts: dict[str, Contact],
    *,
    email: str,
    name: str,
    company: str,
    item: dict,
) -> None:
    """Add one contact-touchpoint from one item. Called once per (item, person)."""
    email = _lc_email(email)
    if not email:
        return
    c = contacts.get(email)
    if c is None:
        c = Contact(email=email, name=name or "", company=company or "", domain=_domain_of(email))
        contacts[email] = c
    # Prefer the longest non-empty name and company we've seen.
    if name and len(name) > len(c.name):
        c.name = name
    if company and len(company) > len(c.company):
        c.company = company

    src = item.get("source") or ""
    c.sources.add(src)
    c.counts_by_source[src] = c.counts_by_source.get(src, 0) + 1

    handled = _is_handled(item)
    if handled:
        c.handled_count += 1
    else:
        c.open_count += 1

    # Date roll-up
    d = item.get("date") or ""
    if d:
        if not c.first_seen or d < c.first_seen:
            c.first_seen = d
        if not c.last_seen or d > c.last_seen:
            c.last_seen = d

    # "Owes reply" — Gmail-only signal (other sources don't carry it).
    if (
        src == "gmail"
        and not handled
        and item.get("last_inbound_at")
        and item.get("garth_owns_last_turn") is False
    ):
        c.owes_reply_count += 1

    mid = item.get("msg_id")
    if mid and mid not in c.item_msg_ids:
        c.item_msg_ids.append(mid)


def derive_contacts(
    items: Iterable[dict],
    *,
    self_email: str = "garth@ayarlabs.com",
) -> list[Contact]:
    """Walk every cached item and produce the contact list.

    `self_email` is excluded — we don't want Garth showing up as his own contact
    (which Otter would otherwise produce since he's both assignee and a guest).
    """
    self_lc = _lc_email(self_email)
    contacts: dict[str, Contact] = {}

    for item in items:
        src = item.get("source") or ""
        if src == "gmail":
            _record(
                contacts,
                email=item.get("sender_email") or "",
                name=item.get("sender_name") or "",
                company=item.get("sender_company") or "",
                item=item,
            )
        elif src == "slack":
            email = item.get("sender_email") or ""
            if not email:
                continue       # most channel messages have no email; skip
            _record(
                contacts,
                email=email,
                name=item.get("sender_name") or "",
                company="",
                item=item,
            )
        elif src == "freshservice":
            _record(
                contacts,
                email=item.get("sender_email") or "",
                name=item.get("sender_name") or "",
                company="",
                item=item,
            )
        elif src == "jira":
            email = item.get("sender_email") or ""
            if not email:
                continue       # Jira GDPR settings often redact reporter email; skip
            _record(
                contacts,
                email=email,
                name=item.get("sender_name") or "",
                company="",
                item=item,
            )
        elif src == "otter":
            # Each meeting attendee (excluding Garth) gets a touchpoint for this AR.
            for ge in item.get("calendar_guest_emails") or []:
                if _lc_email(ge) == self_lc:
                    continue
                _record(
                    contacts,
                    email=ge,
                    name="",                                # Otter doesn't expose guest display name
                    company="",
                    item=item,
                )

    # Strip self from results just in case (e.g. if Garth is in his own Gmail "to" line).
    contacts.pop(self_lc, None)

    out = list(contacts.values())
    # Default sort: most recently active first.
    out.sort(key=lambda c: (c.last_seen or "", c.email), reverse=True)
    return out
