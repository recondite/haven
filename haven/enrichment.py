"""Enrichment pipeline — add structured context to a GmailItem.

Phase 1.3 covers the deterministic enrichment (no LLM):
- sender_company from domain (with KNOWN_COMPANIES map + heuristic)
- garth_recipient_role: to | cc | bcc
- thread_state: last inbound/outbound timestamps + who owes the next turn
- dates_mentioned: regex-extracted dates from body

Phase 1.4 layers LLM-driven enrichment on top:
- sender_title (from signature block)
- mentioned_people / mentioned_orgs (NER from body)
- score: tag, urgency, reply_needed, suggested_reply
"""
from __future__ import annotations

import re

# Known domain → company canonical names. Extended over time as Haven encounters new orgs.
# The LLM signature-parsing pass (Phase 1.4) populates a SQLite cache for everything not here.
KNOWN_COMPANIES: dict[str, str] = {
    "ayarlabs.com": "Ayar Labs",
    "cresa.com": "Cresa",
    "jll.com": "JLL",
    "google.com": "Google",
    "gmail.com": "Gmail",
    "anthropic.com": "Anthropic",
    "linear.app": "Linear",
    "atlassian.net": "Atlassian",
    "atlassian.com": "Atlassian",
    "slack.com": "Slack",
    "freshservice.com": "Freshservice",
    "asana.com": "Asana",
    "otter.ai": "Otter.ai",
    "github.com": "GitHub",
    "microsoft.com": "Microsoft",
    "notion.so": "Notion",
    "stripe.com": "Stripe",
    "zoom.us": "Zoom",
    "salesforce.com": "Salesforce",
}

_DATE_RE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"                                                  # ISO
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                                            # MM/DD/YY
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?"
    r"|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}"
    r"|(?:next|this|last)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"|tomorrow|today|tonight|EOD|EOW"
    r")\b",
    re.I,
)


def company_from_domain(domain: str) -> str:
    """Map an email domain to a human-readable company name.

    Strategy:
      1. Walk up the dotted parent chain looking for a hit in KNOWN_COMPANIES.
      2. Fallback: take the registrable-domain root label (parts[-2]), not parts[0].
         This avoids "email.claude.com" → "Email" and produces "Claude" instead.
    """
    if not domain:
        return ""
    domain = domain.lower()
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in KNOWN_COMPANIES:
            return KNOWN_COMPANIES[candidate]
    # Registrable root: second-to-last label for normal eTLDs (claude.com → "claude").
    # Single-label edge case (no TLD) falls back to that label.
    root = parts[-2] if len(parts) >= 2 else parts[0]
    return root.replace("-", " ").replace("_", " ").title()


def garth_recipient_role(
    user_email: str,
    to: list[dict[str, str]],
    cc: list[dict[str, str]],
) -> str:
    """Return 'to', 'cc', or 'bcc' (where bcc = neither — distribution list, alias, BCC)."""
    if not user_email:
        return ""
    e = user_email.lower()
    if any(r.get("email", "").lower() == e for r in to):
        return "to"
    if any(r.get("email", "").lower() == e for r in cc):
        return "cc"
    return "bcc"


def dates_mentioned(text: str) -> list[str]:
    """Pull date-like tokens out of body text. Returns up to 10 unique strings, original casing."""
    matches = _DATE_RE.findall(text or "")
    return list(dict.fromkeys(matches))[:10]


def derive_thread_state(
    thread_messages: list[dict],
    user_email: str,
) -> dict:
    """Walk a thread's messages and figure out who owes the next reply.

    `thread_messages` is the list returned by gmail.threads.get with metadata format:
      [{ "internalDate": "1714600000000", "payload": { "headers": [{name, value}, ...] }, ... }, ...]
    """
    user_e = (user_email or "").lower()
    last_inbound: dict | None = None  # not from Garth
    last_outbound: dict | None = None  # from Garth

    for msg in thread_messages:
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        import email.utils as _u
        _, addr = _u.parseaddr(headers.get("from", ""))
        addr = addr.lower()
        ts_str = msg.get("internalDate") or "0"
        try:
            ts = int(ts_str)
        except ValueError:
            ts = 0

        record = {"ts": ts, "date": headers.get("date", "")}

        if addr == user_e:
            if last_outbound is None or ts > last_outbound["ts"]:
                last_outbound = record
        else:
            if last_inbound is None or ts > last_inbound["ts"]:
                last_inbound = record

    if last_inbound and last_outbound:
        garth_owns_last_turn = last_outbound["ts"] >= last_inbound["ts"]
    elif last_outbound and not last_inbound:
        garth_owns_last_turn = True
    else:
        garth_owns_last_turn = False  # only inbound, or empty thread

    return {
        "last_inbound_at": last_inbound["date"] if last_inbound else None,
        "last_outbound_at": last_outbound["date"] if last_outbound else None,
        "garth_owns_last_turn": garth_owns_last_turn,
        "thread_message_count": len(thread_messages),
    }
