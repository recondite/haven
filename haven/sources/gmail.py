"""Gmail fetcher — lists messages by query, fetches full content, parses headers + body + attachments."""
from __future__ import annotations

import asyncio
import base64
import email.utils
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from haven import enrichment
from haven.sources.gmail_auth import GmailAuth

# ─── Helpers ─────────────────────────────────────────────
_LINK_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_STYLE_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"\s+")


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    text = _HTML_STYLE_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _parse_addr(value: str) -> tuple[str, str]:
    name, addr = email.utils.parseaddr(value or "")
    return name.strip(), addr.strip().lower()


def _parse_addr_list(value: str) -> list[dict[str, str]]:
    if not value:
        return []
    parsed = email.utils.getaddresses([value])
    return [{"name": n.strip(), "email": a.strip().lower()} for n, a in parsed if a]


def _headers_dict(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _extract_body(payload: dict) -> tuple[str, list[dict]]:
    """Walk the MIME tree, return (body_text, attachments[])."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        filename = part.get("filename") or ""

        if filename:
            attachments.append(
                {
                    "filename": filename,
                    "mime": mime,
                    "size": int(body.get("size", 0)),
                    "attachment_id": body.get("attachmentId"),
                }
            )
        elif mime == "text/plain" and body.get("data"):
            text_parts.append(_decode_b64url(body["data"]))
        elif mime == "text/html" and body.get("data"):
            html_parts.append(_decode_b64url(body["data"]))

        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    if text_parts:
        body_text = "\n".join(text_parts)
    elif html_parts:
        body_text = _strip_html("\n".join(html_parts))
    else:
        body_text = ""
    return body_text.strip(), attachments


# ─── Item ────────────────────────────────────────────────
@dataclass
class GmailItem:
    msg_id: str
    thread_id: str
    history_id: str
    subject: str
    sender_name: str
    sender_email: str
    sender_domain: str
    to: list[dict[str, str]]
    cc: list[dict[str, str]]
    date: str
    snippet: str
    body_text: str
    labels: list[str]
    has_attachment: bool
    attachments: list[dict]
    links: list[str]
    in_reply_to: str | None
    references: list[str]
    deeplink: str = field(default="")
    # Phase 1.3 enrichment fields (deterministic — no LLM):
    sender_company: str = ""
    garth_recipient_role: str = ""           # to | cc | bcc
    last_inbound_at: str | None = None
    last_outbound_at: str | None = None
    garth_owns_last_turn: bool = False
    thread_message_count: int = 0
    dates_mentioned: list[str] = field(default_factory=list)
    # Phase 1.4 LLM-driven fields (filled later):
    sender_title: str = ""
    mentioned_people: list[str] = field(default_factory=list)
    mentioned_orgs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.deeplink and self.thread_id:
            self.deeplink = f"https://mail.google.com/mail/u/0/#inbox/{self.thread_id}"
        if "@" in self.sender_email and not self.sender_domain:
            self.sender_domain = self.sender_email.split("@", 1)[1]

    def summary(self) -> dict[str, Any]:
        """Lightweight payload for SSE — strip body_text and raw fields."""
        return {
            "source": "gmail",
            "msg_id": self.msg_id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "sender_domain": self.sender_domain,
            "sender_company": self.sender_company,
            "sender_title": self.sender_title,
            "garth_recipient_role": self.garth_recipient_role,
            "date": self.date,
            "snippet": self.snippet,
            "labels": self.labels,
            "has_attachment": self.has_attachment,
            "attachment_count": len(self.attachments),
            "link_count": len(self.links),
            "dates_mentioned": self.dates_mentioned,
            "last_inbound_at": self.last_inbound_at,
            "last_outbound_at": self.last_outbound_at,
            "garth_owns_last_turn": self.garth_owns_last_turn,
            "thread_message_count": self.thread_message_count,
            "deeplink": self.deeplink,
        }


# ─── Fetcher ─────────────────────────────────────────────
class GmailFetcher:
    def __init__(self, auth: GmailAuth, queries: list[str]) -> None:
        self.auth = auth
        self.queries = queries
        self._user_email: str | None = None
        self._labels_map: dict[str, str] | None = None
        # Cache of {label_name_lower: label_id} for labels we've created or resolved.
        self._label_ids: dict[str, str] = {}

    async def _service(self):
        """Shared, cached Gmail service (refresh-safe). Delegates to GmailAuth so
        every fetcher reuses one service and one refresh lock."""
        service = await self.auth.get_service()
        if service is None:
            raise RuntimeError("Gmail not authorized — connect Gmail first")
        return service

    async def user_email(self) -> str:
        """Cache and return the authed user's primary email address."""
        if self._user_email is not None:
            return self._user_email
        service = await self._service()
        profile = await asyncio.to_thread(
            lambda: service.users().getProfile(userId="me").execute(http=self.auth.new_http())
        )
        self._user_email = (profile.get("emailAddress") or "").lower()
        return self._user_email

    async def labels_map(self) -> dict[str, str]:
        """Fetch and cache the {label_id: name} map for this account.

        User-defined labels come back from messages.get as opaque IDs like
        'Label_5788543694286495058'. This call resolves them once per session.
        """
        if self._labels_map is not None:
            return self._labels_map
        service = await self._service()
        result = await asyncio.to_thread(
            lambda: service.users().labels().list(userId="me").execute(http=self.auth.new_http())
        )
        self._labels_map = {
            label["id"]: label.get("name", label["id"])
            for label in result.get("labels", []) or []
        }
        return self._labels_map

    async def ensure_label(self, name: str) -> str:
        """Return the Gmail label ID for `name`, creating it on first use.

        Cached on the fetcher instance per label name.
        """
        key = name.lower()
        cached = self._label_ids.get(key)
        if cached is not None:
            return cached
        service = await self._service()

        def _list_labels() -> dict:
            return service.users().labels().list(userId="me").execute(http=self.auth.new_http())

        listing = await asyncio.to_thread(_list_labels)
        for lbl in listing.get("labels", []) or []:
            if (lbl.get("name") or "").lower() == key:
                self._label_ids[key] = lbl["id"]
                return lbl["id"]

        def _create() -> dict:
            return (
                service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute(http=self.auth.new_http())
            )

        created = await asyncio.to_thread(_create)
        self._label_ids[key] = created["id"]
        # Invalidate the labels_map cache so future label translations include this one.
        self._labels_map = None
        return created["id"]

    async def ensure_haven_label(self) -> str:
        """Backward-compat wrapper — every loaded email gets the Haven label."""
        return await self.ensure_label("Haven")

    async def label_messages(self, msg_ids: list[str], label_name: str) -> int:
        """Apply `label_name` to every given message ID in one batchModify call.

        Returns the count of IDs labeled. Adding a label that's already on a
        message is a no-op server-side, so this is safe to call repeatedly.
        """
        if not msg_ids:
            return 0
        label_id = await self.ensure_label(label_name)
        service = await self._service()

        def _batch() -> None:
            service.users().messages().batchModify(
                userId="me",
                body={"ids": msg_ids, "addLabelIds": [label_id]},
            ).execute(http=self.auth.new_http())

        await asyncio.to_thread(_batch)
        return len(msg_ids)

    async def label_with_haven(self, msg_ids: list[str]) -> int:
        """Backward-compat wrapper — keep existing call sites working."""
        return await self.label_messages(msg_ids, "Haven")

    async def _fetch_thread_state(self, thread_id: str, user_email: str) -> dict:
        if not thread_id:
            return {
                "last_inbound_at": None,
                "last_outbound_at": None,
                "garth_owns_last_turn": False,
                "thread_message_count": 0,
            }
        service = await self._service()

        def _do_get() -> dict:
            return (
                service.users()
                .threads()
                .get(
                    userId="me",
                    id=thread_id,
                    format="metadata",
                    metadataHeaders=["From", "Date"],
                )
                .execute(http=self.auth.new_http())
            )

        thread = await asyncio.to_thread(_do_get)
        return enrichment.derive_thread_state(thread.get("messages", []) or [], user_email)

    async def list_message_ids(self, max_per_query: int = 100) -> list[str]:
        service = await self._service()
        seen: set[str] = set()
        ordered: list[str] = []

        for query in self.queries:
            def _do_list(q: str = query) -> dict:
                return (
                    service.users()
                    .messages()
                    .list(userId="me", q=q, maxResults=max_per_query)
                    .execute(http=self.auth.new_http())
                )

            result = await asyncio.to_thread(_do_list)
            for msg in result.get("messages", []) or []:
                mid = msg["id"]
                if mid not in seen:
                    seen.add(mid)
                    ordered.append(mid)
        return ordered

    async def fetch_metadata(self, msg_id: str) -> dict:
        """Cheap header-only fetch. Used by the pre-LLM filter pass so we can decide
        whether an item is worth full-fetching + enriching + scoring without paying
        the cost of the full body / thread.

        Returns a dict shaped like a partial GmailItem.summary() so it plugs into the
        same filter logic.
        """
        service = await self._service()

        def _do_get() -> dict:
            return (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "To", "Cc", "Date"],
                )
                .execute(http=self.auth.new_http())
            )

        msg = await asyncio.to_thread(_do_get)
        payload = msg.get("payload", {}) or {}
        headers = _headers_dict(payload)
        sender_name, sender_email = _parse_addr(headers.get("from", ""))
        # Translate raw label IDs to human-readable names so the filter can match
        # on label names (e.g. "Ayar-ELT") rather than opaque IDs.
        labels_map = await self.labels_map()
        raw_labels = msg.get("labelIds", []) or []
        labels = [labels_map.get(lid, lid) for lid in raw_labels]
        return {
            "msg_id": msg_id,
            "thread_id": msg.get("threadId", ""),
            "subject": headers.get("subject", "(no subject)"),
            "sender_name": sender_name,
            "sender_email": sender_email,
            "sender_domain": sender_email.split("@", 1)[1] if "@" in sender_email else "",
            "to": _parse_addr_list(headers.get("to", "")),
            "cc": _parse_addr_list(headers.get("cc", "")),
            "date": headers.get("date", ""),
            "labels": labels,
        }

    async def fetch_message(self, msg_id: str) -> GmailItem:
        service = await self._service()

        def _do_get() -> dict:
            return (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute(http=self.auth.new_http())
            )

        msg = await asyncio.to_thread(_do_get)
        item = self._parse(msg)
        await self._enrich(item)
        return item

    async def _enrich(self, item: GmailItem) -> None:
        """Apply Phase 1.3 deterministic enrichment in place."""
        user_email = await self.user_email()
        labels_map = await self.labels_map()
        item.labels = [labels_map.get(label_id, label_id) for label_id in item.labels]
        item.sender_company = enrichment.company_from_domain(item.sender_domain)
        item.garth_recipient_role = enrichment.garth_recipient_role(
            user_email, item.to, item.cc
        )
        item.dates_mentioned = enrichment.dates_mentioned(item.body_text)
        thread_state = await self._fetch_thread_state(item.thread_id, user_email)
        item.last_inbound_at = thread_state["last_inbound_at"]
        item.last_outbound_at = thread_state["last_outbound_at"]
        item.garth_owns_last_turn = thread_state["garth_owns_last_turn"]
        item.thread_message_count = thread_state["thread_message_count"]

    def _parse(self, msg: dict) -> GmailItem:
        payload = msg.get("payload", {}) or {}
        headers = _headers_dict(payload)

        sender_name, sender_email = _parse_addr(headers.get("from", ""))
        body_text, attachments = _extract_body(payload)

        references_raw = headers.get("references", "")
        references = [r.strip() for r in references_raw.split() if r.strip()]

        links = list(dict.fromkeys(_LINK_RE.findall(body_text)))[:50]

        return GmailItem(
            msg_id=msg["id"],
            thread_id=msg.get("threadId", ""),
            history_id=str(msg.get("historyId", "")),
            subject=headers.get("subject", "(no subject)"),
            sender_name=sender_name,
            sender_email=sender_email,
            sender_domain=sender_email.split("@", 1)[1] if "@" in sender_email else "",
            to=_parse_addr_list(headers.get("to", "")),
            cc=_parse_addr_list(headers.get("cc", "")),
            date=headers.get("date", ""),
            snippet=msg.get("snippet", ""),
            body_text=body_text,
            labels=msg.get("labelIds", []) or [],
            has_attachment=bool(attachments),
            attachments=attachments,
            links=links,
            in_reply_to=headers.get("in-reply-to"),
            references=references,
        )
