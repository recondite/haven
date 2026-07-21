"""Pre-LLM filter pass — apply deterministic keep/reject rules so the expensive
LLM scoring only runs on items that actually warrant it.

Decisions:
  ACCEPT — bypass any noise classification; force the item visible.
  REJECT — set tag="noise" deterministically without calling the LLM.
  UNCERTAIN — defer to LLM scoring.

Static rules live in `agents/gmail.yaml` (edited by hand). Dynamic block list
(per-sender / per-domain entries Garth adds via the UI) lives in
`data/state/blocked-senders.json` so we can mutate it programmatically without
rewriting the YAML.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from haven import config

log = logging.getLogger(__name__)

CONFIG_PATH = config.AGENTS_CONFIG_DIR / "gmail.yaml"
BLOCKLIST_PATH = config.DATA_DIR / "state" / "blocked-senders.json"
WATCHLIST_PATH = config.DATA_DIR / "state" / "watchlist.json"
_blocklist_lock = threading.Lock()
_watchlist_lock = threading.Lock()


class Decision:
    ACCEPT = "accept"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("agents/gmail.yaml not found — filters disabled")
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.error("Failed to parse gmail.yaml: %s", e)
        return {}


def _lower_set(values) -> set[str]:
    return {str(v).lower() for v in (values or [])}


def is_direct_to(payload: dict[str, Any], self_email: str) -> bool:
    """True if Garth is a direct To: recipient of this item.

    Prefers the enrichment-derived `garth_recipient_role` ("to"|"cc"|"bcc") when
    present — that's what cached/refiltered payloads carry. During the live
    metadata-first filter pass the role isn't computed yet, so fall back to
    scanning the raw `to` list for `self_email`.
    """
    role = (payload.get("garth_recipient_role") or "").lower()
    if role:
        return role == "to"
    self_lc = (self_email or "").lower()
    if not self_lc:
        return False
    to = payload.get("to") or []
    return any((r.get("email") or "").lower() == self_lc for r in to)


# ─── Dynamic block list (UI-managed) ───────────────────────
def _load_blocklist() -> dict:
    if not BLOCKLIST_PATH.exists():
        return {"senders": [], "domains": []}
    try:
        return json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Failed to read blocklist %s: %s", BLOCKLIST_PATH, e)
        return {"senders": [], "domains": []}


def _save_blocklist(data: dict) -> None:
    BLOCKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOCKLIST_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def block_sender(email: str, *, domain_too: bool = False, reason: str = "") -> dict:
    """Add an email (and optionally its domain) to the dynamic block list."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email required")
    with _blocklist_lock:
        data = _load_blocklist()
        senders = {s.get("email", "").lower(): s for s in data.get("senders", [])}
        if email not in senders:
            senders[email] = {
                "email": email,
                "blocked_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            }
        if domain_too and "@" in email:
            domain = email.split("@", 1)[1]
            domains = {d.get("domain", "").lower(): d for d in data.get("domains", [])}
            if domain not in domains:
                domains[domain] = {
                    "domain": domain,
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                }
            data["domains"] = list(domains.values())
        data["senders"] = list(senders.values())
        _save_blocklist(data)
        return data


def is_blocked(sender_email: str, sender_domain: str) -> tuple[bool, str]:
    sender_email = (sender_email or "").lower()
    sender_domain = (sender_domain or "").lower()
    data = _load_blocklist()
    if any(s.get("email", "").lower() == sender_email for s in data.get("senders", [])):
        return True, "blocked sender"
    if sender_domain and any(
        d.get("domain", "").lower() == sender_domain for d in data.get("domains", [])
    ):
        return True, f"blocked domain {sender_domain}"
    return False, ""


# ─── Watchlist (UI-managed keywords that force-keep emails) ─
def _load_watchlist_raw() -> list[str]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        kws = data.get("keywords", [])
        return [k for k in kws if isinstance(k, str) and k.strip()]
    except Exception as e:
        log.error("Failed to read watchlist %s: %s", WATCHLIST_PATH, e)
        return []


def _save_watchlist_raw(keywords: list[str]) -> None:
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(
        json.dumps({"keywords": keywords}, indent=2),
        encoding="utf-8",
    )


def get_watchlist() -> list[str]:
    return _load_watchlist_raw()


def add_watchlist_keyword(keyword: str) -> list[str]:
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("keyword required")
    with _watchlist_lock:
        kws = _load_watchlist_raw()
        if keyword.lower() not in [k.lower() for k in kws]:
            kws.append(keyword)
            _save_watchlist_raw(kws)
        return kws


def remove_watchlist_keyword(keyword: str) -> list[str]:
    target = (keyword or "").strip().lower()
    with _watchlist_lock:
        kws = _load_watchlist_raw()
        new = [k for k in kws if k.lower() != target]
        if len(new) != len(kws):
            _save_watchlist_raw(new)
        return new


def watchlist_match(
    subject: str = "",
    sender_email: str = "",
    sender_domain: str = "",
    sender_name: str = "",
) -> str | None:
    """Return the first watchlist keyword that matches subject or sender info.

    Matching strategy:
      - subject and email **local part**: case-insensitive **whole-word** match
        (`\\b<kw>\\b`). Whole-word keeps "permit" from matching "supermarket" and,
        critically, keeps a short keyword like "JLL" from matching the random
        local part of a no-reply address
        (`no-reply-fchcfojll7...@mail.anthropic.com`).
      - sender **domain** + **name**: case-insensitive **substring** match. So a
        watchlist entry of "clearsulting" matches "josh@clearsulting.com" (and the
        bare domain), and "ayar" catches "ayarlabs.com". That's the intent for
        company/vendor watchlists — list the brand once and every variant address
        from that vendor is caught.

    Returns the first matching keyword (in watchlist insertion order) or None.
    """
    kws = _load_watchlist_raw()
    if not kws:
        return None

    local, _, email_domain = (sender_email or "").lower().partition("@")
    # Substring targets: the domain (from either arg) and the display name.
    loose_blob = " ".join(
        s for s in (sender_domain.lower(), email_domain, sender_name.lower()) if s
    )

    for kw in kws:
        kw_lc = kw.lower()
        pat = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
        # Subject + email local part: whole-word (no gibberish false positives).
        if subject and pat.search(subject):
            return kw
        if local and pat.search(local):
            return kw
        # Domain + name: substring (partial/prefix allowed).
        if loose_blob and kw_lc in loose_blob:
            return kw
    return None


def travel_match(
    cfg: dict,
    subject: str = "",
    sender_domain: str = "",
) -> str | None:
    """Return a short reason if this looks like a travel notification, else None.

    Matches when the sender domain ends with a configured travel domain (so
    `email.cathaypacific.com` matches `cathaypacific.com`) OR the subject matches
    a configured travel subject pattern. Domains and patterns live in the
    `travel` block of agents/gmail.yaml.
    """
    travel = cfg.get("travel") or {}
    domains = _lower_set(travel.get("domains"))
    sd = (sender_domain or "").lower()
    if sd and domains:
        for d in domains:
            if sd == d or sd.endswith("." + d):
                return f"travel domain {d}"
    if subject:
        for pattern in travel.get("subject_patterns") or []:
            try:
                if re.search(pattern, subject, re.IGNORECASE):
                    return f"travel subject {pattern!r}"
            except re.error as e:
                log.warning("Invalid travel subject_pattern %r: %s", pattern, e)
    return None


def urgent_approval_match(
    cfg: dict, subject: str = "", sender_domain: str = "", sender_email: str = ""
) -> str | None:
    """Return a reason if this is a priority approval request that must always be
    surfaced as URGENT, else None.

    Requires the sender domain to end with a configured `urgent_approvals` domain,
    then matches on EITHER:
      - the sender mailbox (local part) being in `approval_senders` — a mailbox
        that only ever sends approval requests (Coupa's `approvals@`), so the
        subject wording is irrelevant and can never cause a miss; OR
      - the subject matching one of the approval `subject_patterns` (so a Coupa
        *status* notice like "PO received" — no approval language — is NOT swept
        in).
    The poll pipeline pins tag="approval", urgency="urgent" on the
    is_priority_approval flag.
    """
    ua = cfg.get("urgent_approvals") or {}
    domains = _lower_set(ua.get("domains"))
    sd = (sender_domain or "").lower()
    domain_ok = bool(sd and domains and any(sd == d or sd.endswith("." + d) for d in domains))
    if not domain_ok:
        return None
    # Sender-mailbox rule: subject-independent, so wording changes can't cause a miss.
    local = (sender_email or "").split("@", 1)[0].lower()
    if local and local in _lower_set(ua.get("approval_senders")):
        return f"approval sender ({sender_email})"
    for pattern in ua.get("subject_patterns") or []:
        try:
            if re.search(pattern, subject or "", re.IGNORECASE):
                return f"urgent approval ({sd}): {pattern!r}"
        except re.error as e:
            log.warning("Invalid urgent_approvals subject_pattern %r: %s", pattern, e)
    return None


def apply_filter(payload: dict[str, Any]) -> tuple[str, str, dict]:
    """Run hard rules against an enriched item payload.

    Returns: (decision, reason, flags)
      - decision: "accept" | "reject" | "uncertain"
      - reason: short human-readable text
      - flags: extra metadata to merge into the payload (is_elt, from_ayar, etc.)
    """
    cfg = load_config()
    never_keep = cfg.get("never_keep") or {}
    keep = cfg.get("keep") or {}
    reject = cfg.get("reject") or {}

    sender = (payload.get("sender_email") or "").lower()
    sender_domain = (payload.get("sender_domain") or "").lower()
    sender_name = (payload.get("sender_name") or "").lower()
    subject = payload.get("subject") or ""
    flags: dict = {}

    # ─── Ignore label (highest priority — beats everything) ─
    # Anything Garth has tagged with the Gmail "ignore" label is never imported,
    # regardless of sender/subject/watchlist. The query already excludes
    # `-label:ignore`, but re-check here so cached refilters and force re-polls
    # (which bypass the rejection store) still drop it.
    item_labels_lc = {(l or "").lower() for l in (payload.get("labels") or [])}
    if "ignore" in item_labels_lc:
        return Decision.REJECT, "ignore label", flags

    # ─── Dynamic block list (highest priority) ──────────────
    blocked, blk_reason = is_blocked(sender, sender_domain)
    if blocked:
        return Decision.REJECT, blk_reason, flags

    # ─── Watchlist (force-keep on subject OR sender match) ──
    # Subject uses whole-word, sender_email/domain/name use substring — so an
    # entry like "clearsulting" catches anyone @clearsulting.com without
    # accidentally matching unrelated text.
    matched_kw = watchlist_match(
        subject=subject,
        sender_email=sender,
        sender_domain=sender_domain,
        sender_name=sender_name,
    )
    if matched_kw:
        flags["watchlist_match"] = matched_kw
        return Decision.ACCEPT, f"watchlist: {matched_kw}", flags

    # ─── Label-based keep (beats never_keep) ─────────────────
    # Anything the user has tagged with a configured label in Gmail is auto-kept,
    # regardless of subject/sender patterns. e.g. "Ayar-ELT" emails.
    keep_labels = {l.lower() for l in (keep.get("labels") or [])}
    item_labels = {(l or "").lower() for l in (payload.get("labels") or [])}
    matched = keep_labels & item_labels
    if matched:
        label_name = next(iter(matched))
        flags["matched_label"] = label_name
        # Treat ELT-labeled threads as ELT urgency-floor.
        if "elt" in label_name:
            flags["is_elt"] = True
        return Decision.ACCEPT, f"label match: {label_name}", flags

    # ─── Keep only when directly addressed to Garth ─────────
    # IT-helpdesk / Freshservice ticket blasts (it-helpdesk@ayarlabs.com) CC Garth
    # on every ticket thread. Keep them only when he's a direct To: recipient;
    # otherwise reject as noise. Runs before the generic ayarlabs.com domain accept
    # so these don't get auto-kept just for being @ayarlabs.com. Block/watchlist/
    # label keeps above still win.
    direct_cfg = cfg.get("keep_only_if_direct") or {}
    direct_senders = _lower_set(direct_cfg.get("senders"))
    direct_domains = _lower_set(direct_cfg.get("domains"))
    if (sender and sender in direct_senders) or (sender_domain and sender_domain in direct_domains):
        self_email = (cfg.get("self_email") or "").lower()
        if is_direct_to(payload, self_email):
            flags["direct_to_garth"] = True
            return Decision.ACCEPT, "direct To: recipient (keep_only_if_direct)", flags
        return Decision.REJECT, "it-helpdesk: not directly addressed to Garth", flags

    # ─── Priority approvals (force-keep, pinned URGENT) ─────
    # Coupa (and similar) approval requests. They arrive from do-not-reply
    # addresses, so they must be caught here — before never_keep and the reject
    # sender_patterns — or they'd be dropped as noise. The poll pipeline pins
    # tag="approval" and urgency="urgent" on the is_priority_approval flag.
    approval_reason = urgent_approval_match(
        cfg, subject=subject, sender_domain=sender_domain, sender_email=sender
    )
    if approval_reason:
        flags["is_priority_approval"] = True
        return Decision.ACCEPT, approval_reason, flags

    # ─── Travel (force-keep + tag="travel") ─────────────────
    # Airline/hotel/car-rental confirmations and itineraries. These come from
    # noreply/notifications addresses, so they must be caught here — before
    # never_keep (receipts) and the reject sender_patterns below — or they'd be
    # dropped as noise. The poll pipeline pins tag="travel" on the is_travel flag.
    travel_reason = travel_match(cfg, subject=subject, sender_domain=sender_domain)
    if travel_reason:
        flags["is_travel"] = True
        return Decision.ACCEPT, travel_reason, flags

    # ─── Never-keep (structural noise — beats every accept rule) ──
    # Calendar invites, OOO/PTO, password resets, receipts. These are noise
    # regardless of who sent them (yes, even from Ayar people).
    for pattern in never_keep.get("subject_patterns") or []:
        try:
            if re.search(pattern, subject, re.IGNORECASE):
                return Decision.REJECT, f"never-keep subject {pattern!r}", flags
        except re.error as e:
            log.warning("Invalid never_keep subject_pattern %r: %s", pattern, e)
    for pattern in never_keep.get("sender_patterns") or []:
        try:
            if re.search(pattern, sender, re.IGNORECASE):
                return Decision.REJECT, f"never-keep sender {pattern!r}", flags
        except re.error as e:
            log.warning("Invalid never_keep sender_pattern %r: %s", pattern, e)

    # ─── Hard accept ───────────────────────────────────────
    keep_domains = _lower_set(keep.get("domains"))
    if sender_domain and sender_domain in keep_domains:
        flags["from_ayar"] = sender_domain == "ayarlabs.com"
        return Decision.ACCEPT, f"sender domain {sender_domain}", flags

    elt = _lower_set(keep.get("elt"))
    if sender and sender in elt:
        flags["is_elt"] = True
        return Decision.ACCEPT, "ELT member", flags

    team = _lower_set(keep.get("team"))
    if sender and sender in team:
        flags["is_team"] = True
        return Decision.ACCEPT, "team member", flags

    approved = _lower_set(keep.get("approved_external"))
    if sender and sender in approved:
        flags["approved_external"] = True
        return Decision.ACCEPT, "approved external sender", flags

    # If anyone @ayarlabs.com is on To/Cc, treat the thread as a real Ayar thread
    # that includes Garth — keep it, even if external sender.
    to_cc = (payload.get("to") or []) + (payload.get("cc") or [])
    if any(
        (r.get("email") or "").lower().endswith("@ayarlabs.com") for r in to_cc
    ):
        flags["ayar_in_thread"] = True
        return Decision.ACCEPT, "ayarlabs.com person on to/cc", flags

    # ─── Hard reject ───────────────────────────────────────
    # First: check whether this sender's domain is in the noreply allowlist.
    # If so, defer to LLM judgment regardless of static patterns — real business
    # actions can come from noreply addresses (Google Drive shares etc.).
    noreply_allow = _lower_set(keep.get("noreply_allowlist_domains"))
    if sender_domain and sender_domain in noreply_allow:
        flags["noreply_allowlisted"] = True
        return Decision.UNCERTAIN, f"noreply allowlist: {sender_domain}", flags

    for pattern in reject.get("sender_patterns") or []:
        try:
            if re.search(pattern, sender, re.IGNORECASE):
                return Decision.REJECT, f"sender pattern {pattern!r}", flags
        except re.error as e:
            log.warning("Invalid sender_pattern %r: %s", pattern, e)

    for pattern in reject.get("subject_patterns") or []:
        try:
            if re.search(pattern, subject, re.IGNORECASE):
                return Decision.REJECT, f"subject pattern {pattern!r}", flags
        except re.error as e:
            log.warning("Invalid subject_pattern %r: %s", pattern, e)

    reject_domains = _lower_set(reject.get("domains"))
    if sender_domain and sender_domain in reject_domains:
        return Decision.REJECT, f"reject domain {sender_domain}", flags

    return Decision.UNCERTAIN, "no static rule matched", flags


def auto_approve_from_history(payload: dict[str, Any]) -> bool:
    """Has Garth historically engaged with this sender? (cheap heuristic, no DB.)
    Currently uses the per-item thread state already in the payload:
      - last_outbound_at present means Garth has replied somewhere in this thread.
    Returns True if the sender should be treated as approved going forward.
    """
    cfg = load_config()
    if not cfg.get("auto_approve_replied_senders", True):
        return False
    return bool(payload.get("last_outbound_at"))


REJECT_OVERRIDE_PAYLOAD = {
    "tag": "noise",
    "urgency": "low",
    "reply_needed": False,
    "reply_reason": "",
    "summary": "",
    "suggested_action": "",
    "suggested_reply": "",
}
# (travel notifications are force-kept upstream and tagged "travel" by the poll pipeline)
