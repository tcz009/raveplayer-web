# -*- coding: utf-8 -*-
"""UK Raves resolution performance guards (1.6.65 web-perf).

Two changes keep a warm UK Raves room off the network:

* successful provider classifications are memoised per page, so a reload does
  not do one cache read (and one pool task) per row;
* /go/mix hops run on a keep-alive connection, so forty hops are forty HTTP
  requests rather than forty TLS handshakes.

Everything here is offline: the resolver's connection is replaced with a fake
and ``redirect_target`` is monkeypatched.
"""

import os

import pytest

from core import cache, ukraves

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "more-sources")


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    ukraves._drop_connection()
    yield
    cache.clear_memory()
    ukraves._drop_connection()


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def _rows(limit=6):
    return ukraves.parse_page(_fixture("ukraves-downloads.html"), 1)[0][:limit]


def test_described_page_cache_skips_every_row_on_reload(monkeypatch):
    rows = _rows()
    calls = []

    def target(mix_id):
        calls.append(mix_id)
        return "https://www.mediafire.com/file/%s/x.mp3" % mix_id

    monkeypatch.setattr(ukraves, "redirect_target", target)
    first = ukraves.describe_many(rows)
    assert [row["provider"]["kind"] for row in first] == ["mediafire"] * len(rows)
    assert len(calls) == len(rows)

    second = ukraves.describe_many(rows)
    assert [row["provider"]["kind"] for row in second] == ["mediafire"] * len(rows)
    assert len(calls) == len(rows), "a warm page must not resolve any row again"
    # Titles stay the caller's current parse, only the provider is cached.
    assert [row["title"] for row in second] == [row["title"] for row in rows]


def test_described_page_cache_does_not_hide_an_unavailable_row(monkeypatch):
    rows = _rows(4)
    broken = rows[0]["id"]
    calls = []
    state = {"fixed": False}

    def target(mix_id):
        calls.append(mix_id)
        if mix_id == broken and not state["fixed"]:
            return ""
        return "https://www.mediafire.com/file/%s/x.mp3" % mix_id

    monkeypatch.setattr(ukraves, "redirect_target", target)
    first = ukraves.describe_many(rows)
    assert first[0]["provider"]["kind"] == "unavailable"
    assert first[1]["provider"]["kind"] == "mediafire"
    before = len(calls)

    state["fixed"] = True
    second = ukraves.describe_many(rows)
    assert second[0]["provider"]["kind"] == "mediafire"
    assert len(calls) == before + 1, "only the previously unavailable row is retried"


def test_describe_cache_is_keyed_by_the_id_set(monkeypatch):
    rows = _rows(4)
    calls = []
    monkeypatch.setattr(ukraves, "redirect_target",
                        lambda mix_id: calls.append(mix_id) or
                        "https://www.mediafire.com/file/%s/x.mp3" % mix_id)
    ukraves.describe_many(rows)
    assert len(calls) == 4
    # A different page (different ids) must resolve its own rows.
    other = rows[:2] + [
        {"id": "999999", "title": "Other", "go_url": "https://www.ukraves.co.uk/go/mix/999999"},
        {"id": "999998", "title": "Other 2", "go_url": "https://www.ukraves.co.uk/go/mix/999998"},
    ]
    ukraves.describe_many(other)
    # A different id set is a different page: it resolves its own rows rather
    # than borrowing another page's map.
    assert len(calls) == 8


class _FakeResponse(object):
    def __init__(self, status=302, location=""):
        self.status = status
        self._location = location
        self.length = 0

    def getheader(self, name):
        return self._location if name.lower() == "location" else None

    def read(self, amount=None):
        return b""


class _FakeConnection(object):
    def __init__(self, location="https://www.mediafire.com/file/x/y.mp3"):
        self.location = location
        self.requests = 0
        self.closed = 0

    def request(self, method, target, headers=None):
        self.requests += 1
        self.last_target = target

    def getresponse(self):
        return _FakeResponse(302, self.location)

    def close(self):
        self.closed += 1


def test_connection_is_created_once_per_thread(monkeypatch):
    created = []

    class _Factory(_FakeConnection):
        def __init__(self, *args, **kwargs):
            _FakeConnection.__init__(self)
            created.append(self)

    monkeypatch.setattr(ukraves.http_client, "HTTPSConnection", _Factory)
    first = ukraves._connection()
    second = ukraves._connection()
    assert first is second
    assert len(created) == 1


def test_redirect_hops_reuse_one_keep_alive_connection(monkeypatch):
    conn = _FakeConnection()

    monkeypatch.setattr(ukraves, "_connection", lambda: conn)
    for mix_id in ("7524", "7523", "7522"):
        assert ukraves._fetch_location(
            "https://www.ukraves.co.uk/go/mix/%s" % mix_id) == conn.location
    assert conn.requests == 3
    assert conn.closed == 0, "a successful hop must keep the socket open"
    assert conn.last_target.startswith("/go/mix/")


def test_redirect_hop_reconnects_once_after_a_dropped_socket(monkeypatch):
    class _Broken(_FakeConnection):
        def getresponse(self):
            raise ukraves.http_client.RemoteDisconnected("closed")

    broken = _Broken()
    healthy = _FakeConnection("https://www.mediafire.com/file/ok/y.mp3")
    queue = [broken, healthy]

    monkeypatch.setattr(ukraves, "_connection", lambda: queue.pop(0))
    assert ukraves._fetch_location(
        "https://www.ukraves.co.uk/go/mix/7524") == healthy.location
    assert healthy.requests == 1


def test_redirect_hop_keeps_the_old_urllib_path_for_other_hosts(monkeypatch):
    called = []
    monkeypatch.setattr(ukraves, "_fetch_location_once",
                        lambda url: called.append(url) or "")
    assert ukraves._fetch_location("https://example.com/go/mix/1") == ""
    assert called == ["https://example.com/go/mix/1"]
