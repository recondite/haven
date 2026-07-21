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
from haven.db import cursor_store

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

HISTORY_LIMIT = 50
CHANNEL_CONCURRENCY = 3            # easy on Slack tier-3 limits
MAX_IMS_TO_SCAN = 40               # cap IM scan to avoid rate limits
SEARCH_PAGE_SIZE = 100
# DM discovery via search: bound work + tolerate poll gaps / search-index lag.
MAX_DM_CANDIDATES = 25             # distinct DM channels probed per poll
DM_SEARCH_MAX_PAGES = 5           # search pages before giving up (page cap)
DM_FLOOR_OVERLAP = 600            # re-scan the last 10min each poll (lag/gap safety)


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
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        """Lazily create + reuse one client so a poll's many calls share a
        connection pool instead of paying a fresh TCP+TLS handshake each time."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, params: dict | None = None, *, use_bot: bool = False, retried: bool = False) -> dict:
        token = self.bot_token if use_bot else self.user_token
        if not token:
            raise RuntimeError(f"Slack token missing for {'bot' if use_bot else 'user'}")
        headers = {"Authorization": f"Bearer {token}"}
        c = self._http()
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

    async def search_paged(self, query: str, *, stop_ts: float = 0.0,
                           max_pages: int = DM_SEARCH_MAX_PAGES,
                           page_size: int = SEARCH_PAGE_SIZE) -> list[dict]:
        """Paginated search.messages, newest-first, stopping once a page's oldest
        match is at/older than `stop_ts` (so steady state reads one page). Fails
        safe to whatever was collected — a bad/scope-less query degrades DMs to
        empty rather than crashing the poll."""
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            try:
                j = await self._call("search.messages", {
                    "query": query, "count": str(page_size),
                    "sort": "timestamp", "sort_dir": "desc", "page": str(page)})
            except Exception as e:
                log.warning("search.messages page %d (%r) failed: %s", page, query, e)
                break
            block = j.get("messages") or {}
            matches = block.get("matches") or []
            out.extend(matches)
            if not matches:
                break
            try:
                if float(matches[-1].get("ts") or 0) <= stop_ts:
                    break
            except (TypeError, ValueError):
                pass
            pages = int((block.get("paging") or {}).get("pages") or page)
            if page >= pages:
                break
            page += 1
        return out


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
        """Surface unread 1:1 (im) and group (mpim) DMs.

        `conversations.list` returns no unread count (and stale metadata), and
        `client.counts` is blocked for user tokens, so we can't cheaply ask "which
        DMs are unread". Instead:
          1. ONE `search.messages` sweep (`after:<floor>`) discovers recent inbound
             messages across ALL conversation types — the only aggregate signal a
             user token has. It covers group DMs natively and returns message
             content, so we never call `conversations.history` (immune to Slack's
             history throttle).
          2. Group hits by channel; for each candidate, `conversations.info` gives
             the accurate `last_read` — the read cursor that works for BOTH im and
             mpim (mpim has no unread badge anywhere). Emit only messages newer
             than `last_read`; if `last_read` is 0/stale, fall back to the search
             floor so we never dump ancient history.

        A persisted `dm_search_floor` cursor (advanced to now-OVERLAP each poll)
        keeps steady state to ~1 search call; a missed poll leaves the old floor,
        so the gap is re-scanned. Re-surfacing is prevented by slack_poll's
        existing msg_id dedup against the cache.
        """
        await self._ensure_team_domain()
        raw = cursor_store.get_cursor("slack", "dm_search_floor")
        try:
            floor = float(raw) if raw else since
        except (TypeError, ValueError):
            floor = since
        poll_start = time.time()

        after = datetime.fromtimestamp(max(floor, 0.0), tz=timezone.utc).strftime("%Y-%m-%d")
        query = f"after:{after}"
        if self.identity_id:
            query += f" -from:<@{self.identity_id}>"
        matches = await self.client.search_paged(query, stop_ts=floor)

        by_channel: dict[str, list[dict]] = {}
        for m in matches:
            ch = m.get("channel") or {}
            cid = ch.get("id") or ""
            is_im = m.get("type") == "im" or bool(ch.get("is_im")) or cid.startswith("D")
            is_mpim = bool(ch.get("is_mpim")) or cid.startswith("G")
            if not cid or not (is_im or is_mpim):
                continue
            uid = m.get("user") or ""
            if not uid or uid == self.identity_id:  # inbound only (search -from: is a coarse guard)
                continue
            try:
                if float(m.get("ts") or 0) <= floor:
                    continue
            except (TypeError, ValueError):
                continue
            if self._should_skip_subtype(m):
                continue
            by_channel.setdefault(cid, []).append(m)

        # Newest-active channels first, bounded (protects the info-call budget).
        candidates = sorted(
            by_channel.items(),
            key=lambda kv: max(float(x.get("ts") or 0) for x in kv[1]),
            reverse=True,
        )[:MAX_DM_CANDIDATES]

        sem = asyncio.Semaphore(CHANNEL_CONCURRENCY)
        items: list[SlackItem] = []

        async def one(cid: str, msgs: list[dict]) -> None:
            info: dict = {}
            async with sem:
                try:
                    info = await self.client.conversation_info(cid)
                except Exception as e:
                    log.warning("conversations.info(%s) failed, gating on floor: %s", cid, e)
            is_im = bool(info.get("is_im")) or cid.startswith("D")
            ch_type = "im" if is_im else "mpim"
            ch_name = info.get("name") or ("DM" if is_im else "Group DM")
            # 1:1 IMs expose an accurate unread badge — trust a zero and skip.
            if is_im and info.get("unread_count_display") == 0:
                return
            try:
                last_read = float(info.get("last_read") or 0)
            except (TypeError, ValueError):
                last_read = 0.0
            gate = last_read if last_read else floor   # last_read=0/stale -> bounded floor
            for m in sorted(msgs, key=lambda x: float(x.get("ts") or 0)):
                try:
                    if float(m.get("ts") or 0) <= gate:
                        continue
                except (TypeError, ValueError):
                    continue
                it = self._make_item(
                    m, cid, ch_name, ch_type,
                    is_dm=True, is_mention=self._has_self_mention(m),
                    is_watched_channel=False, is_watched_user=False,
                )
                if it:
                    items.append(it)

        await asyncio.gather(*[one(cid, msgs) for cid, msgs in candidates])
        cursor_store.set_cursor("slack", "dm_search_floor", f"{poll_start - DM_FLOOR_OVERLAP:.6f}")
        log.info("Slack DMs: %d candidate channels, %d unread items", len(candidates), len(items))
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
        try:
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
        finally:
            await self.client.aclose()
