"""fetch_dms: search-discovery + last_read gating for IMs and MPIMs (no network)."""
import asyncio

import pytest

from haven.sources import slack as slack_mod
from haven.sources.slack import SlackFetcher

SELF = "USELF"
OTHER = "UOTHER"


class FakeClient:
    """Stand-in for SlackClient: canned search + per-channel info."""
    def __init__(self, matches, infos):
        self._matches = matches
        self._infos = infos
        self.search_calls = 0
        self.info_calls = []

    async def team_domain(self):
        return "ayarlabs"

    async def search_paged(self, query, *, stop_ts=0.0, max_pages=5, page_size=100):
        self.search_calls += 1
        return self._matches

    async def conversation_info(self, cid):
        self.info_calls.append(cid)
        return self._infos.get(cid, {})

    async def aclose(self):
        pass


def _msg(cid, user, ts, *, is_im=False, is_mpim=False, mtype="message"):
    return {"type": mtype, "user": user, "ts": ts, "text": f"msg {ts}",
            "username": user, "channel": {"id": cid, "is_im": is_im, "is_mpim": is_mpim}}


@pytest.fixture(autouse=True)
def _cfg_and_cursor(monkeypatch):
    monkeypatch.setattr(slack_mod, "load_config",
                        lambda: {"identity": {"user_id": SELF}, "never_keep": []})
    monkeypatch.setattr(slack_mod.cursor_store, "get_cursor", lambda *a, **k: None)  # no floor -> use `since`
    monkeypatch.setattr(slack_mod.cursor_store, "set_cursor", lambda *a, **k: None)


def _run_fetch(matches, infos, since=50.0):
    f = SlackFetcher(client=FakeClient(matches, infos))
    items = asyncio.run(f.fetch_dms(since))
    return f.client, items


def test_im_and_mpim_unread_surface_others_dropped():
    matches = [
        _msg("D1", OTHER, "200.0", is_im=True),     # IM unread (last_read 100) -> keep
        _msg("D2", OTHER, "300.0", is_im=True),     # IM but unread_count_display==0 -> skip channel
        _msg("G1", OTHER, "250.0", is_mpim=True),   # group DM, no unread badge, last_read 100 -> keep
        _msg("D3", OTHER, "120.0", is_im=True),     # <= last_read 150 -> drop
        _msg("D1", SELF, "260.0", is_im=True),      # self-authored -> exclude
        _msg("C1", OTHER, "400.0"),                 # not a DM -> exclude
        _msg("D1", OTHER, "40.0", is_im=True),      # below floor(50) -> exclude
    ]
    infos = {
        "D1": {"is_im": True, "last_read": "100.0", "unread_count_display": 2},
        "D2": {"is_im": True, "last_read": "0", "unread_count_display": 0},
        "G1": {"is_im": False, "last_read": "100.0"},   # MPIM: no unread_count field
        "D3": {"is_im": True, "last_read": "150.0", "unread_count_display": 1},
    }
    _, items = _run_fetch(matches, infos)
    ids = sorted(i.msg_id for i in items)
    assert ids == ["D1:200.0", "G1:250.0"]
    types = {i.channel_id: i.channel_type for i in items}
    assert types == {"D1": "im", "G1": "mpim"}


def test_last_read_zero_falls_back_to_floor_not_ancient():
    # last_read unknown (0) -> gate on the search floor, so nothing older than it leaks.
    matches = [
        _msg("D9", OTHER, "500.0", is_im=True),   # > floor -> keep
        _msg("D9", OTHER, "10.0", is_im=True),    # < floor -> drop even though last_read=0
    ]
    infos = {"D9": {"is_im": True, "last_read": "0"}}  # no unread badge, stale read cursor
    _, items = _run_fetch(matches, infos, since=100.0)
    assert [i.msg_id for i in items] == ["D9:500.0"]


def test_no_dm_matches_makes_no_info_calls():
    client, items = _run_fetch([_msg("C1", OTHER, "400.0")], {})  # channel only
    assert items == []
    assert client.info_calls == []       # never probe when nothing to gate
    assert client.search_calls == 1      # exactly one discovery sweep
