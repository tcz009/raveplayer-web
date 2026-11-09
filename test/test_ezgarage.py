# -*- coding: utf-8 -*-
"""DNBForum EZ Garage / Mediafire: resolver, cache, API, auth and downloads.

Everything here is offline: the Mediafire page fetch and the stream proxy's
connection layer are replaced with fixtures/fakes.
"""

import os
import threading
import time
import urllib.parse

import pytest

import app as app_module
from core import ezgarage

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SET_ID = "72aswcrx4hlw2sh"
SIGNED = (
    "https://download1346.mediafire.com/"
    "40kvzr3mwokgqHEiZDXv-It7QLsnz5tSwo0snMJAWOcom8eqoqy4pCFSRIjEHqiU2cyE9Tk1bXeT6X309z"
    "BU1PSed2yebDbQO3tfLcxsL-i738sVS-XAx3rOJpuISQmDcjsEOmPBZ2DQYJNk8ZFvlB3cPeuZodsHt1HoeZ"
    "uF8UpV1plv/72aswcrx4hlw2sh/DJ+EZ%40LaCosaNostra.mp3?token=abc123&expires=1799999999"
)


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def _fake_fetch(monkeypatch, body, status=200, headers=None):
    calls = []

    def fake(url):
        calls.append(url)
        return status, dict(headers or {}), body

    monkeypatch.setattr(ezgarage, "_fetch_page", fake)
    return calls


@pytest.fixture(autouse=True)
def isolated_ezgarage(monkeypatch):
    ezgarage.clear_memory()
    monkeypatch.setattr(ezgarage, "MIN_GAP", 0.0)
    yield
    ezgarage.clear_memory()


# --------------------------------------------------------------------------- data + parsing


def test_sets_are_the_23_curated_mediafire_ids():
    assert len(ezgarage.SETS) == 23
    assert len({row["id"] for row in ezgarage.SETS}) == 23
    assert ezgarage.SETS[0] == {
        "id": "72aswcrx4hlw2sh", "title": "DJ EZ @ La Cosa Nostra"}
    assert ezgarage.SETS[-1] == {
        "id": "zsla8z4f8d7v721", "title": "DJ EZ @ United Dance 2003"}
    assert ezgarage.SETS[20]["id"] == "vzq2nwtgruj"
    assert ezgarage.SETS[14]["title"] == "DJ EZ - @ Back 2 95 Nov 2011"


def test_signed_url_comes_from_the_download_anchor():
    url = ezgarage.signed_url_from_page(_fixture("mediafire-ez.html"))
    assert url == SIGNED
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname == "download1346.mediafire.com"
    assert parts.path.endswith(".mp3")


def test_scrambled_data_attribute_is_decoded():
    url = ezgarage.signed_url_from_page(_fixture("mediafire-ez-scrambled.html"))
    assert url == (
        "https://download2244.mediafire.com/abcDEF123/72aswcrx4hlw2sh/"
        "DJ+EZ%40LaCosaNostra.mp3?token=xyz")


@pytest.mark.parametrize("body", [
    "<a href='https://www.mediafire.com/file/72aswcrx4hlw2sh/x.mp3'>x</a>",
    "<a href='https://mediafire.com/x.mp3'>x</a>",
    "<a href='http://download1.mediafire.com/x.mp3'>x</a>",
    "<a href='https://download1.mediafire.com/x.zip'>x</a>",
    "<a href='https://example.com/x.mp3'>x</a>",
    "<a href='https://download1.mediafire.com.evil.test/x.mp3'>x</a>",
    "<html>no link here</html>",
    "",
])
def test_parser_rejects_non_mediafire_or_non_direct_urls(body):
    assert ezgarage.signed_url_from_page(body) == ""


# --------------------------------------------------------------------------- cache + limits


def test_resolve_caches_the_signed_url_for_two_hours(monkeypatch):
    calls = _fake_fetch(monkeypatch, _fixture("mediafire-ez.html"))
    first = ezgarage.resolve(SET_ID)
    assert first == SIGNED
    assert calls == [ezgarage.page_url(SET_ID)]
    assert ezgarage.resolve(SET_ID) == SIGNED
    assert len(calls) == 1
    cached = ezgarage._CACHE[SET_ID]
    assert cached["url"] == SIGNED
    assert 0 < cached["expires"] - time.monotonic() <= ezgarage.TTL

    cached["expires"] = time.monotonic() - 1
    assert ezgarage.resolve(SET_ID) == SIGNED
    assert len(calls) == 2


def test_parse_failure_is_negatively_cached(monkeypatch):
    calls = _fake_fetch(monkeypatch, "<html>changed</html>")
    with pytest.raises(ezgarage.MediafireFormatChanged):
        ezgarage.resolve(SET_ID)
    with pytest.raises(ezgarage.MediafireFormatChanged):
        ezgarage.resolve(SET_ID)
    assert len(calls) == 1
    assert ezgarage._CACHE[SET_ID]["error"] is ezgarage.MediafireFormatChanged


def test_429_sets_backoff_and_fails_fast(monkeypatch):
    calls = _fake_fetch(monkeypatch, "", status=429, headers={"Retry-After": "7"})
    with pytest.raises(ezgarage.MediafireRateLimited) as info:
        ezgarage.resolve(SET_ID)
    assert info.value.code == 503
    assert info.value.retry_after == 7
    assert ezgarage._BACKOFF_UNTIL > time.monotonic()
    with pytest.raises(ezgarage.MediafireRateLimited):
        ezgarage.resolve(SET_ID)
    assert len(calls) == 1
    assert ezgarage._CACHE[SET_ID]["error"] is ezgarage.MediafireRateLimited


def test_page_fetch_uses_the_curated_url_and_browser_ua(monkeypatch):
    captured = {}

    class Response(object):
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, size=-1):
            return b"<html>ok</html>"

        def close(self):
            pass

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value
                               for key, value in request.header_items()}
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ezgarage.urllib.request, "urlopen", fake_urlopen)
    status, headers, body = ezgarage._fetch_page(ezgarage.page_url(SET_ID))
    assert status == 200
    assert body == "<html>ok</html>"
    assert captured["url"] == (
        "https://www.mediafire.com/file/72aswcrx4hlw2sh/x.mp3")
    assert "Mozilla/5.0" in captured["headers"]["user-agent"]
    assert captured["headers"]["referer"] == ezgarage.SOURCE_URL
    assert captured["timeout"] == ezgarage.TIMEOUT


def test_404_is_negatively_cached(monkeypatch):
    calls = _fake_fetch(monkeypatch, "", status=404)
    with pytest.raises(ezgarage.MediafireUnavailable) as info:
        ezgarage.resolve(SET_ID)
    assert info.value.code == 404
    with pytest.raises(ezgarage.MediafireUnavailable) as again:
        ezgarage.resolve(SET_ID)
    assert again.value.code == 404
    assert len(calls) == 1


def test_unknown_set_does_not_touch_the_network(monkeypatch):
    calls = _fake_fetch(monkeypatch, _fixture("mediafire-ez.html"))
    with pytest.raises(ezgarage.UnknownSet):
        ezgarage.resolve("nope")
    assert calls == []


def test_concurrent_resolves_share_one_fetch(monkeypatch):
    calls = []
    release = threading.Event()

    def fake(url):
        calls.append(url)
        release.wait(2)
        return 200, {}, _fixture("mediafire-ez.html")

    monkeypatch.setattr(ezgarage, "_fetch_page", fake)
    results = []
    start = threading.Barrier(3)

    def worker():
        start.wait()
        results.append(ezgarage.resolve(SET_ID))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    # Give the first worker time to enter the resolver lock, then let it finish.
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == [SIGNED, SIGNED]
    assert calls == [ezgarage.page_url(SET_ID)]


# --------------------------------------------------------------------------- API shape + auth


def test_room_has_23_rows_with_real_art_play_and_download(web):
    payload = web.get("/api/ezgarage").get_json()
    assert payload["title"] == "EZ Garage Sets"
    assert "DNBForum" in payload["sub"]
    assert payload["source"] == ezgarage.SOURCE_URL
    assert len(payload["items"]) == 23
    for row, source in zip(payload["items"], ezgarage.SETS):
        assert row["label"] == source["title"]
        assert row["sub"] == "DJ EZ · DNBForum"
        assert row["art"] == "/static/art/djez.png"
        assert row["play"] == "/api/ezgarage/%s/stream" % source["id"]
        assert row["file"] == "/api/ezgarage/%s/file" % source["id"]
        assert row["download"] == {"source": "mediafire", "id": source["id"]}


def test_ezgarage_routes_require_auth(anon):
    for path in ("/api/ezgarage",
                 "/api/ezgarage/%s/stream" % SET_ID,
                 "/api/ezgarage/%s/file" % SET_ID):
        assert anon.get(path).status_code == 401


def test_taxonomy_and_packs_hub_include_ezgarage(web):
    menu = web.get("/api/menu").get_json()
    section = next(row for row in menu["sections"] if row["id"] == "ezgarage")
    assert section["label"] == "EZ Garage Sets"
    assert section["path"] == "/api/ezgarage"
    assert section["icon"].endswith("/djez.png")
    packs = next(group for group in menu["rail"] if group["id"] == "packs")
    assert {"id": "ezgarage", "path": "/api/ezgarage"} in packs["children"]

    hub = web.get("/api/packs").get_json()
    assert "EZ Garage Sets" in [item["label"] for item in hub["items"]]
    rail = web.get("/api/rail/packs").get_json()
    assert "EZ Garage Sets" in [item["label"] for item in rail["items"]]


# --------------------------------------------------------------------------- streaming


class _FakeResponse(object):
    def __init__(self, body=b"ID3" + b"a" * 40, content_type="audio/mpeg"):
        self.status = 200
        self._body = body
        self._offset = 0
        self._content_type = content_type

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return self._content_type
        return None

    def read1(self, size=-1):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeConnection(object):
    def close(self):
        pass


def _patch_stream(monkeypatch):
    calls = []

    def fake_open_stream(url, headers, deadline):
        calls.append((url, headers))
        return _FakeConnection(), _FakeResponse(), url

    monkeypatch.setattr(app_module, "_open_stream", fake_open_stream)
    return calls


def test_stream_resolves_then_proxies_same_origin(web, monkeypatch):
    monkeypatch.setattr(app_module.ezgarage, "resolve", lambda set_id: SIGNED)
    calls = _patch_stream(monkeypatch)
    response = web.get("/api/ezgarage/%s/stream" % SET_ID,
                       headers={"Range": "bytes=0-3"})
    assert response.status_code == 200
    assert response.data.startswith(b"ID3")
    assert response.headers["Content-Type"].startswith("audio/")
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert calls[0][0] == SIGNED
    assert calls[0][1]["Range"] == "bytes=0-3"


def test_file_route_streams_as_attachment(web, monkeypatch):
    monkeypatch.setattr(app_module.ezgarage, "resolve", lambda set_id: SIGNED)
    _patch_stream(monkeypatch)
    response = web.get("/api/ezgarage/%s/file" % SET_ID)
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "DJ-EZ-La-Cosa-Nostra" in disposition
    assert response.data.startswith(b"ID3")


def test_stream_maps_typed_resolver_errors(web, monkeypatch):
    def limited(set_id):
        raise ezgarage.MediafireRateLimited("slow down", retry_after=7)

    monkeypatch.setattr(app_module.ezgarage, "resolve", limited)
    response = web.get("/api/ezgarage/%s/stream" % SET_ID)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "7"
    assert response.get_json()["code"] == "ezgarage"

    assert web.get("/api/ezgarage/nope/stream").status_code == 404


# --------------------------------------------------------------------------- downloads


def test_mediafire_direct_urls_are_eligible_but_pages_are_not():
    allowed = app_module._download_allowed
    assert allowed("mediafire", SIGNED)[0]
    assert allowed("ezgarage", SIGNED)[0]
    for source in ("mediafire", "ezgarage"):
        for url in (
            "https://www.mediafire.com/file/72aswcrx4hlw2sh/x.mp3",
            "https://mediafire.com/file/72aswcrx4hlw2sh/x.mp3",
            "https://download1.mediafire.com/x.zip",
            "https://example.com/x.mp3",
            "https://download1.mediafire.com.evil.test/x.mp3",
        ):
            assert not allowed(source, url)[0], (source, url)
    assert not allowed("mediafire", "https://archive.org/download/x/x.mp3")[0]


def test_download_api_rejects_arbitrary_mediafire_urls(web):
    payload = {"source": "mediafire", "url": SIGNED, "title": "x"}
    assert web.post("/api/downloads", json=payload).status_code == 400
    assert web.post("/api/downloads/jobs", json=payload).status_code == 400
    unknown = {"source": "mediafire", "source_id": "nope", "title": "x"}
    assert web.post("/api/downloads", json=unknown).status_code == 400
    assert web.post("/api/downloads/jobs", json=unknown).status_code == 400


def test_background_job_resolves_the_curated_set_at_job_time(web, monkeypatch):
    resolved = []

    def fake_resolve(set_id):
        resolved.append(set_id)
        return SIGNED

    monkeypatch.setattr(app_module.ezgarage, "resolve", fake_resolve)
    seen = []

    def fake_chunks(url, destination, max_bytes, seconds, should_cancel=None):
        seen.append(url)
        yield {"started": True, "total": 100}
        with open(destination, "wb") as handle:
            handle.write(b"x" * 100)
        yield {"done": True, "size": 100}

    monkeypatch.setattr(app_module, "_download_chunks", fake_chunks)
    response = web.post("/api/downloads/jobs", json={
        "source": "mediafire", "source_id": SET_ID, "title": "DJ EZ test"})
    assert response.status_code == 202, response.get_data(as_text=True)
    job_id = response.get_json()["id"]
    deadline = time.time() + 5
    job = None
    while time.time() < deadline:
        job = web.get("/api/downloads/jobs/%s" % job_id).get_json()["job"]
        if job["state"] == "completed":
            break
        time.sleep(0.02)
    assert job and job["state"] == "completed", job
    assert resolved == [SET_ID]
    assert seen == [SIGNED]

    listing = web.get("/api/downloads").get_json()
    item = next(row for row in listing["items"] if row["label"] == "DJ EZ test")
    assert item["sub"].startswith("100 B")
    assert "DNBForum / Mediafire" in item["sub"]
    assert item["art"].endswith("/djez.png")

    # The stable page URL, not the expiring signed URL, is the dedupe key.
    again = web.post("/api/downloads/jobs", json={
        "source": "mediafire", "source_id": SET_ID, "title": "DJ EZ test"})
    assert again.status_code == 200
    assert again.get_json()["already"] is True


def test_generic_mediafire_page_is_single_flight_and_cached(monkeypatch):
    """UK Raves uses the same lock/cache as the curated EZ list."""
    page = "https://www.mediafire.com/file/abc123def456/mix.MP3/file"
    calls = []
    release = threading.Event()

    def fake(url):
        calls.append(url)
        release.wait(2)
        return 200, {}, _fixture("mediafire-ez.html")

    monkeypatch.setattr(ezgarage, "_fetch_page", fake)
    results = []
    start = threading.Barrier(3)

    def worker():
        start.wait()
        results.append(ezgarage.resolve_page(page))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == [SIGNED, SIGNED]
    assert calls == [page]
    assert ezgarage.resolve_page(page) == SIGNED
    assert calls == [page]
