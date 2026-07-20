"""Gmail cache reconcile: read/resolved items drop; handled items are kept."""
from haven.services.gmail_poll import resolved_ids


def _c(mid, **kw):
    return {"msg_id": mid, **kw}


def test_read_item_is_pruned():
    cached = [_c("a"), _c("b"), _c("c")]
    live = ["a", "c"]                      # b was read in Gmail -> gone from query
    assert resolved_ids(cached, live) == ["b"]


def test_handled_item_is_kept():
    # d left the unread set because Haven archived it (handled) — keep for toggle.
    cached = [_c("d", handled_at=123.0)]
    assert resolved_ids(cached, live_ids=[]) == []


def test_still_live_item_not_pruned():
    cached = [_c("a")]
    assert resolved_ids(cached, ["a"]) == []


def test_nothing_when_all_live():
    cached = [_c("a"), _c("b")]
    assert resolved_ids(cached, ["a", "b"]) == []


def test_all_resolved():
    cached = [_c("a"), _c("b")]
    assert set(resolved_ids(cached, [])) == {"a", "b"}
