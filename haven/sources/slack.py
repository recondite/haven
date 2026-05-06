"""Slack fetcher — pulls inbound messages addressed to Garth.

Four signal sources:
  1. DMs (1:1 IM) and group DMs (mpim) — every non-Garth message
  2. Any channel where someone @-mentions Garth
  3. Watched channels (e.g. #elt-2026) — every inbound message
  4. Watched users (e.g. Mark Wade) — every message from them, anywhere

Design notes:
  - We do NOT call `chat.getPermalink` per message — Slack permalinks are
    deterministic from team_domain + channel_id + ts.
  - We do NOT call `users.info` — token may not have `users:read`. We
    enrich names lazily from the message dict only.
  - Channel names for IMs come from the conversations.list response;
    for channels found via search, we fall back to the channel id.
  - 429s are handled with `Retry-After`-respecting single retry.
  - We bound the IM scan to the most-recent N IMs (sorted by latest activity)
    to keep total API calls under Slack's tier-3/tier-4 limits.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml

from haven import config

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

HISTORY_LIMIT = 50
CHANNEL_CONCURRENCY = 3            # easy on Slack tier-3 limits
MAX_IMS_TO_SCAN = 40               # cap IM scan to avoid rate limits
SEARCH_PAGE_SIZE = 100


@dataclass
class SlackItem:
    msg_id: str                       # "<channel_id>:<ts>"
    channel_id: str
    channel_name: str
    channel_type: str                 # "im" | "mpim" | "channel" | "group"
    ts: str
    thread_ts: str | None
    user_id: str
    user_name: str
    user_real_name: str
    text: str
    permalink: str
    date: str                         # ISO from ts
    is_dm: bool
    is_mention: bool
    is_watched_channel: bool
    is_watched_user: bool
    reactions: list[dict] = field(default_factory=list)
    attachments_count: int = 0

    def summary(self) -> dict[str, Any]:
        if self.channel_type == "im":
            subject = f"DM from {self.user_real_name or self.user_name or self.user_id}"
        elif self.channel_type == "mpim":
            subject = f"Group DM from {self.user_real_name or self.user_name or self.user_id}"
        else:
            subject = f"#{self.channel_name}: {self.user_real_name or self.user_name or self.user_id}"
        snippet = (self.text or "").strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        return {
            "source": "slack",
            "msg_id": self.msg_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "channel_type": self.channel_type,
            "ts": self.ts,
            "thread_ts": self.thread_ts,
            "subject": subject,
            "sender_id": self.user_id,
            "sender_name": self.user_real_name or self.user_name or self.user_id,
            "sender_email": "",                          # users:read scope not granted
            "sender_company": "",
            "date": self.date,
            "snippet": snippet,
            "body_text": self.text,
            "deeplink": self.permalink,
            "is_dm": self.is_dm,
            "is_mention": self.is_mention,
            "is_watched_channel": self.is_watched_channel,
            "is_watched_user": self.is_watched_user,
            "reactions_count": sum(r.get("count", 0) for r in self.reactions),
            "attachments_count": self.attachments_count,
            "labels": [],
            "has_attachment": self.attachments_count > 0,
            "attachment_count": self.attachments_count,
        }


def load_config() -> dict:
    p = config.AGENTS_CONFIG_DIR / "slack.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _ts_to_iso(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _synth_permalink(team_domain: str, channel_id: str, ts: str) -> str:
    """Slack permalinks follow this stable pattern — no API call needed."""
    if not team_domain or not channel_id or not ts:
        return ""
    p_part = "p" + ts.replace(".", "")
    return f"https://{team_domain}.slack.com/archives/{channel_id}/{p_part}"


class SlackClient:
    def __init__(self) -> None:
        self.user_token = config.SLACK_USER_TOKEN
        self.bot_token = config.SLACK_BOT_TOKEN
        self._self_user_id: str | None = None
        self._team_domain: str | None = None

    async def _call(self, method: str, params: dict | None = None, *, use_bot: bool = False, retried: bool = False) -> dict:
        token = self.bot_token if use_bot else self.user_token
        if not token:
            raise RuntimeError(f"Slack token missing for {'bot' if use_bot else 'user'}")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{SLACK_API}/{method}", headers=headers, params=params or {})

        if r.status_code == 429 and not retried:
            wait = float(r.headers.get("Retry-After", "1"))
            log.warning("Slack %s 429 — sleeping %.1fs then retrying", method, wait)
            await asyncio.sleep(wait + 0.5)
            return await self._call(method, params, use_bot=use_bot, retried=True)

        if r.status_code != 200:
            raise RuntimeError(f"Slack {method} HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"Slack {method} error: {j.get('error')}")
        return j

    async def auth_test(self) -> dict:
        j = await self._call("auth.test")
        self._self_user_id = j.get("user_id")
        # Slack's auth.test returns `team` (workspace name) and `url` like
        # https://ayarlabs.slack.com/ — extract the subdomain.
        url = j.get("url") or ""
        if url.startswith("https://"):
            host = url[len("https://"):].rstrip("/")
            self._team_domain = host.split(".", 1)[0] if "." in host else None
        return j

    async def self_user_id(self) -> str:
        if not self._self_user_id:
            await self.auth_test()
        return self._self_user_id or ""

    async def team_domain(self) -> str:
        if not self._team_domain:
            await self.auth_test()
        return self._team_domain or ""

    async def list_dms(self) -> list[dict]:
        """Returns IM and MPIM channels only. Filters defensively by id prefix."""
        out: list[dict] = []
        cursor = ""
        while True:
            params: dict[str, str] = {"types": "im,mpim", "limit": "200", "exclude_archived": "true"}
            if cursor:
                params["cursor"] = cursor
            j = await self._call("conversations.list", params)
            for ch in j.get("channels", []):
                cid = ch.get("id", "")
                if cid.startswith("D") or cid.startswith("G"):
                    out.append(ch)
            cursor = (j.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return out

    async def conversation_info(self, channel_id: str) -> dict:
        """Single channel info — used to check unread state on watched channels."""
        j = await self._call("conversations.info", {"channel": channel_id})
        return j.get("channel") or {}

    async def history(self, channel_id: str, oldest: float | None = None, limit: int = HISTORY_LIMIT) -> list[dict]:
        params: dict[str, Any] = {"channel": channel_id, "limit": str(limit)}
        if oldest:
            params["oldest"] = f"{oldest:.6f}"
        try:
            j = await self._call("conversations.history", params)
        except RuntimeError as e:
            log.warning("history(%s) skipped: %s", channel_id, e)
            return []
        return j.get("messages", []) or []

    async def search_messages(self, query: str, max_results: int = SEARCH_PAGE_SIZE) -> list[dict]:
        try:
            j = await self._call("search.messages", {"query": query, "count": str(max_results), "sort": "timestamp"})
        except Exception as e:
            log.warning("search.messages(%r) failed: %s", query, e)
            return []
        return (j.get("messages") or {}).get("matches") or []


class SlackFetcher:
    def __init__(self, client: SlackClient | None = None) -> None:
        self.client = client or SlackClient()
        self.cfg = load_config()
        self.identity_id: str = (self.cfg.get("identity") or {}).get("user_id", "")
        self.watched_channels: list[dict] = self.cfg.get("watched_channels") or []
        self.watched_users: list[dict] = self.cfg.get("watched_users") or []
        self.lookback = int(self.cfg.get("initial_lookback_seconds", 7 * 86400))
        self.never_keep_subtypes = set(self.cfg.get("never_keep") or [])
        self._team_domain: str = ""

    def _is_inbound(self, msg: dict) -> bool:
        return bool(msg.get("user")) and msg.get("user") != self.identity_id

    def _has_self_mention(self, msg: dict) -> bool:
        return f"<@{self.identity_id}>" in (msg.get("text") or "")

    def _should_skip_subtype(self, msg: dict) -> bool:
        st = msg.get("subtype")
        if st and st in self.never_keep_subtypes:
            return True
        if st == "file_share" and not (msg.get("text") or "").strip():
            return "file_share_only" in self.never_keep_subtypes
        return False

    def _make_item(
        self,
        msg: dict,
        channel_id: str,
        channel_name: str,
        channel_type: str,
        *,
        is_dm: bool,
        is_mention: bool,
        is_watched_channel: bool,
        is_watched_user: bool,
    ) -> SlackItem | None:
        ts = msg.get("ts") or ""
        if not ts:
            return None
        user_id = msg.get("user") or ""
        # Slack search results put a `username` directly in the match payload
        user_name = msg.get("username") or msg.get("user") or ""
        user_real = (msg.get("user_profile") or {}).get("real_name", "") or msg.get("username", "")
        watched_user_ids = {w["id"] for w in self.watched_users}

        return SlackItem(
            msg_id=f"{channel_id}:{ts}",
            channel_id=channel_id,
            channel_name=channel_name or channel_id,
            channel_type=channel_type,
            ts=ts,
            thread_ts=msg.get("thread_ts"),
            user_id=user_id,
            user_name=user_name,
            user_real_name=user_real,
            text=(msg.get("text") or "").strip(),
            permalink=_synth_permalink(self._team_domain, channel_id, ts),
            date=_ts_to_iso(ts),
            is_dm=is_dm,
            is_mention=is_mention,
            is_watched_channel=is_watched_channel,
            is_watched_user=is_watched_user or (user_id in watched_user_ids),
            reactions=msg.get("reactions") or [],
            attachments_count=len(msg.get("files") or []),
        )

    async def _ensure_team_domain(self) -> None:
        if not self._team_domain:
            self._team_domain = await self.client.team_domain()

    async def fetch_dms(self, since: float) -> list[SlackItem]:
        """Pull only unread DMs. Slack tells us which IMs have new messages
        and where the read cursor is — no point fetching history Garth has
        already seen."""
        await self._ensure_team_domain()
        chans = await self.client.list_dms()
        # Keep only DMs with unread messages
        unread = [c for c in chans if (c.get("unread_count_display") or c.get("unread_count") or 0) > 0]
        # Cap as a safety net (e.g. if a user nukes "mark all as read" later)
        if len(unread) > MAX_IMS_TO_SCAN:
            unread.sort(key=lambda c: float((c.get("latest") or {}).get("ts") or "0"), reverse=True)
            unread = unread[:MAX_IMS_TO_SCAN]

        log.info("Slack DMs: %d total, %d with unread", len(chans), len(unread))
        if not unread:
            return []

        sem = asyncio.Semaphore(CHANNEL_CONCURRENCY)
        items: list[SlackItem] = []

        async def one(ch: dict) -> None:
            cid = ch["id"]
            ch_type = "im" if ch.get("is_im") or cid.startswith("D") else "mpim"
            ch_name = ch.get("name") or ("DM" if ch_type == "im" else "Group DM")
            # Use Slack's last_read cursor as oldest — only pull truly unread.
            try:
                last_read = float(ch.get("last_read") or "0")
            except Exception:
                last_read = 0.0
            oldest = max(last_read, since) if last_read else since
            async with sem:
                msgs = await self.client.history(cid, oldest=oldest)
            for m in msgs:
                if not self._is_inbound(m) or self._should_skip_subtype(m):
                    continue
                # Defense in depth: drop messages at/before last_read just in case
                try:
                    if last_read and float(m.get("ts") or "0") <= last_read:
                        continue
                except Exception:
                    pass
                it = self._make_item(
                    m, cid, ch_name, ch_type,
                    is_dm=True, is_mention=False,
                    is_watched_channel=False, is_watched_user=False,
                )
                if it:
                    items.append(it)

        await asyncio.gather(*[one(c) for c in unread])
        return items

    async def fetch_watched_channels(self, since: float) -> list[SlackItem]:
        """Pull from watched channels only when they have unread, using the
        last_read cursor as the lower bound."""
        await self._ensure_team_domain()
        sem = asyncio.Semaphore(CHANNEL_CONCURRENCY)
        items: list[SlackItem] = []

        async def one(wc: dict) -> None:
            cid = wc["id"]
            cname = wc.get("name") or cid
            try:
                info = await self.client.conversation_info(cid)
            except Exception as e:
                log.warning("conversations.info(%s) failed: %s", cid, e)
                return
            unread = (info.get("unread_count_display") or info.get("unread_count") or 0)
            if not unread:
                log.debug("Watched channel %s: no unread, skipping", cname)
                return
            try:
                last_read = float(info.get("last_read") or "0")
            except Exception:
                last_read = 0.0
            oldest = max(last_read, since) if last_read else since
            log.info("Watched channel %s: %d unread since %.0f", cname, unread, oldest)
            async with sem:
                msgs = await self.client.history(cid, oldest=oldest)
            for m in msgs:
                if not self._is_inbound(m) or self._should_skip_subtype(m):
                    continue
                try:
                    if last_read and float(m.get("ts") or "0") <= last_read:
                        continue
                except Exception:
                    pass
                it = self._make_item(
                    m, cid, cname, "channel",
                    is_dm=False, is_mention=self._has_self_mention(m),
                    is_watched_channel=True, is_watched_user=False,
                )
                if it:
                    items.append(it)

        await asyncio.gather(*[one(wc) for wc in self.watched_channels])
        return items

    async def fetch_mentions(self, since: float) -> list[SlackItem]:
        await self._ensure_team_domain()
        query = f"<@{self.identity_id}>"
        matches = await self.client.search_messages(query, max_results=SEARCH_PAGE_SIZE)
        return self._matches_to_items(matches, since, mention=True, watched_user=False)

    async def fetch_from_watched_users(self, since: float) -> list[SlackItem]:
        await self._ensure_team_domain()
        items: list[SlackItem] = []
        for w in self.watched_users:
            query = f"from:<@{w['id']}>"
            matches = await self.client.search_messages(query, max_results=SEARCH_PAGE_SIZE)
            items.extend(self._matches_to_items(matches, since, mention=False, watched_user=True))
        return items

    def _matches_to_items(self, matches: list[dict], since: float, *, mention: bool, watched_user: bool) -> list[SlackItem]:
        out: list[SlackItem] = []
        for m in matches:
            ts_str = m.get("ts") or ""
            try:
                if float(ts_str or 0) < since:
                    continue
            except Exception:
                continue
            if self._should_skip_subtype(m):
                continue
            ch = m.get("channel") or {}
            cid = ch.get("id") or ""
            cname = ch.get("name") or cid
            is_im = bool(ch.get("is_im")) or cid.startswith("D")
            is_mpim = bool(ch.get("is_mpim")) or cid.startswith("G")
            ch_type = "im" if is_im else ("mpim" if is_mpim else "channel")
            # For DMs found via search, Garth might be the receiver of his own DM thread —
            # only keep messages from someone else.
            user_id = m.get("user") or ""
            if user_id == self.identity_id:
                continue
            it = self._make_item(
                m, cid, cname, ch_type,
                is_dm=is_im or is_mpim,
                is_mention=mention or self._has_self_mention(m),
                is_watched_channel=False,
                is_watched_user=watched_user,
            )
            if it:
                out.append(it)
        return out

    async def fetch_all(self, since: float | None = None) -> list[SlackItem]:
        if since is None:
            since = time.time() - self.lookback
        await self._ensure_team_domain()
        results = await asyncio.gather(
            self.fetch_dms(since),
            self.fetch_watched_channels(since),
            self.fetch_mentions(since),
            self.fetch_from_watched_users(since),
            return_exceptions=True,
        )
        merged: dict[str, SlackItem] = {}
        for r in results:
            if isinstance(r, Exception):
                log.error("Slack sub-fetch failed: %s", r)
                continue
            for item in r:
                if item.msg_id in merged:
                    p = merged[item.msg_id]
                    p.is_dm = p.is_dm or item.is_dm
                    p.is_mention = p.is_mention or item.is_mention
                    p.is_watched_channel = p.is_watched_channel or item.is_watched_channel
                    p.is_watched_user = p.is_watched_user or item.is_watched_user
                else:
                    merged[item.msg_id] = item
        return sorted(merged.values(), key=lambda i: float(i.ts), reverse=True)
