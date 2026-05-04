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


def watchlist_match(text: str) -> str | None:
    """Return the first watchlist keyword found in `text` (case-insensitive whole-word).

    The whole-word boundary `\\b` keeps "permit" from matching e.g. "supermarket"
    but allows "permits", "permitted", etc. (those still contain "permit" but not
    as a whole word — `\\bpermit\\b` matches the standalone form). Adjust per
    keyword by adding variants explicitly to the list.
    """
    if not text:
        return None
    kws = _load_watchlist_raw()
    if not kws:
        return None
    for kw in kws:
        try:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                return kw
        except re.error:
            continue
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
    subject = payload.get("subject") or ""
    flags: dict = {}

    # ─── Dynamic block list (highest priority) ──────────────
    blocked, blk_reason = is_blocked(sender, sender_domain)
    if blocked:
        return Decision.REJECT, blk_reason, flags

    # ─── Watchlist (force-keep on subject keyword) ──────────
    matched_kw = watchlist_match(subject)
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
