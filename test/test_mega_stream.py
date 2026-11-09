# -*- coding: utf-8 -*-
"""Offline tests for the MEGA decrypting stream proxy.

Everything here is injected: the resolver call and the storage HTTP client are
fakes, so no test touches DNS, MEGA's API or a storage node.  The AES-CTR
fixture below is a fixed ciphertext for a fixed 32-byte MEGA key, computed once
with pycryptodome; it pins the key derivation, IV and counter layout.
"""

import base64
import logging
import time
import urllib.parse

import pytest
from Crypto.Cipher import AES

import app as app_module
from core.resolver import mega
from core.resolver import mega_stream
from core.resolver.base import Resolved
from core.resolver import service as resolver_service


# --------------------------------------------------------------------------- fixture

KEY_BYTES = bytes(range(32))
KEY_B64 = base64.urlsafe_b64encode(KEY_BYTES).decode("ascii").rstrip("=")
HANDLE = "raByiTib"
LINK = "https://mega.nz/file/%s#%s" % (HANDLE, KEY_B64)
OTHER_LINK = "https://mega.nz/file/zzzzzzzz#%s" % KEY_B64

FIXTURE_PLAINTEXT = (
    b"MEGA stream known fixture: 0123456789abcdef" * 4)[:64]
FIXTURE_CIPHERTEXT = bytes.fromhex(
    "d0c79f29d753202659b4c792ec7c52d745972419489bcb28f043e9659f9840b1"
    "f6a65b0eaea478b5dc3b844e5a67b13d41b6f1faefdb4c92d8a46ae4425f9ac5")

PLAINTEXT = bytes((index * 37 + 11) % 256 for index in range(4096))
_AES_KEY = bytes(left ^ right for left, right in zip(KEY_BYTES[:16],
                                                     KEY_BYTES[16:32]))
_NONCE = KEY_BYTES[16:24]
CIPHERTEXT = AES.new(_AES_KEY, AES.MODE_CTR, nonce=_NONCE,
                     initial_value=0).encrypt(PLAINTEXT)

SECRET = "test-mega-token-secret"


@pytest.fixture(autouse=True)
def fresh_slots():
    mega_stream.reset_slots()
    mega.clear_memory()
    yield
    mega_stream.reset_slots()
    mega.clear_memory()


# --------------------------------------------------------------------------- fakes


class FakeResponse(object):
    def __init__(self, status, body=b"", headers=None):
        self.status = int(status)
        self._body = body
        self._headers = dict(headers or {})
        self._pos = 0
        self.closed = False

    def getheader(self, name):
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def read1(self, size):
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeStorage(object):
    """A storage node that serves ciphertext and honours byte ranges."""

    def __init__(self, ciphertext, statuses=None, ignore_range=False,
                 uses_per_url=None):
        self.ciphertext = ciphertext
        self.statuses = dict(statuses or {})
        self.ignore_range = ignore_range
        self.uses_per_url = uses_per_url
        self.calls = []
        self._uses = {}

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "timeout": timeout})
        if url in self.statuses:
            return FakeResponse(self.statuses[url], b"", {})
        if self.uses_per_url is not None:
            used = self._uses.get(url, 0) + 1
            self._uses[url] = used
            if used > self.uses_per_url:
                return FakeResponse(403, b"", {})
        range_header = headers.get("Range")
        if range_header and not self.ignore_range:
            start, end = _parse_upstream_range(range_header,
                                               len(self.ciphertext))
            body = self.ciphertext[start:end + 1]
            return FakeResponse(206, body, {
                "Content-Range": "bytes %d-%d/%d" % (
                    start, end, len(self.ciphertext)),
                "Content-Length": str(len(body)),
                "Content-Type": "application/octet-stream",
            })
        return FakeResponse(200, self.ciphertext, {
            "Content-Length": str(len(self.ciphertext)),
            "Content-Type": "application/octet-stream",
        })


def _parse_upstream_range(value, total):
    assert value.startswith("bytes=")
    first, _, last = value[6:].partition("-")
    start = int(first)
    end = int(last) if last else total - 1
    return start, min(end, total - 1)


class FakeResolver(object):
    def __init__(self, urls, size=len(PLAINTEXT), name="set.mp3"):
        self.urls = list(urls)
        self.size = size
        self.name = name
        self.calls = []

    def __call__(self, link):
        self.calls.append(link)
        index = min(len(self.calls) - 1, len(self.urls) - 1)
        return {"url": self.urls[index], "name": self.name,
                "size": self.size, "source": "mega"}


def _collect(plan):
    chunks = []
    try:
        for chunk in plan.body:
            chunks.append(chunk)
    finally:
        plan.close()
    return b"".join(chunks)


def _engine(resolver, storage, **kwargs):
    return mega_stream.MegaStreamer(resolve=resolver, opener=storage, **kwargs)


# --------------------------------------------------------------------------- AES-CTR


def test_known_aes_ctr_fixture_decrypts_from_the_start():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/fixture"],
                            size=len(FIXTURE_PLAINTEXT), name="fixture.mp3")
    storage = FakeStorage(FIXTURE_CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK)
    assert plan.status == 200
    assert _collect(plan) == FIXTURE_PLAINTEXT
    assert plan.headers["Content-Type"] == "audio/mpeg"
    assert plan.headers["Content-Length"] == str(len(FIXTURE_PLAINTEXT))
    assert plan.headers["Accept-Ranges"] == "bytes"


def test_known_aes_ctr_fixture_decrypts_from_an_unaligned_offset():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/fixture"],
                            size=len(FIXTURE_PLAINTEXT), name="fixture.m4a")
    storage = FakeStorage(FIXTURE_CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK, "bytes=17-33")
    assert plan.status == 206
    assert _collect(plan) == FIXTURE_PLAINTEXT[17:34]
    # The upstream request starts on the AES block boundary and the proxy
    # discards the 1-byte prefix.
    assert storage.calls[0]["headers"]["Range"] == "bytes=16-33"


def test_round_trip_of_a_long_stream():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/long"])
    storage = FakeStorage(CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK)
    assert _collect(plan) == PLAINTEXT


# --------------------------------------------------------------------------- ranges


@pytest.mark.parametrize("header,start,end", [
    ("bytes=0-15", 0, 15),
    ("bytes=5-20", 5, 20),
    ("bytes=1001-1010", 1001, 1010),
    ("bytes=4000-4095", 4000, 4095),
    ("bytes=4000-", 4000, 4095),
    ("bytes=-100", 3996, 4095),
])
def test_range_mapping_start_mid_end(header, start, end):
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/range"])
    storage = FakeStorage(CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK, header)
    assert plan.status == 206
    body = _collect(plan)
    assert body == PLAINTEXT[start:end + 1]
    assert plan.headers["Content-Range"] == "bytes %d-%d/4096" % (start, end)
    assert plan.headers["Content-Length"] == str(end - start + 1)
    expected_upstream_start = start - (start % 16)
    assert storage.calls[0]["headers"]["Range"] == (
        "bytes=%d-%d" % (expected_upstream_start, end)
        if end is not None else "bytes=%d-" % expected_upstream_start)


def test_upstream_ignoring_range_falls_back_to_skipping():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/norange"])
    storage = FakeStorage(CIPHERTEXT, ignore_range=True)
    plan = _engine(resolver, storage).plan(LINK, "bytes=1001-1010")
    assert plan.status == 206
    assert _collect(plan) == PLAINTEXT[1001:1011]
    assert plan.headers["Content-Range"] == "bytes 1001-1010/4096"


def test_upstream_partial_range_starting_mid_block_is_aligned_by_the_cipher():
    class UnalignedStorage(object):
        def __init__(self, ciphertext):
            self.ciphertext = ciphertext
            self.calls = []

        def __call__(self, url, headers, timeout):
            self.calls.append(dict(headers))
            body = self.ciphertext[5:21]
            return FakeResponse(206, body, {
                "Content-Range": "bytes 5-20/%d" % len(self.ciphertext),
                "Content-Length": str(len(body)),
            })

    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/unaligned"])
    storage = UnalignedStorage(CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK, "bytes=5-20")
    assert plan.status == 206
    assert _collect(plan) == PLAINTEXT[5:21]
    assert plan.headers["Content-Range"] == "bytes 5-20/4096"


def test_unsatisfiable_ranges_are_rejected():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/x"])
    storage = FakeStorage(CIPHERTEXT)
    streamer = _engine(resolver, storage)
    with pytest.raises(mega_stream.RangeNotSatisfiable):
        streamer.plan(LINK, "bytes=4096-")
    with pytest.raises(mega_stream.RangeNotSatisfiable):
        streamer.plan(LINK, "bytes=10-5")
    with pytest.raises(mega_stream.RangeNotSatisfiable):
        streamer.plan(LINK, "bytes=0-1,4-5")
    with pytest.raises(mega_stream.RangeNotSatisfiable):
        streamer.plan(LINK, "bytes=abc")
    assert storage.calls == []


def test_range_parsing_helpers():
    assert mega_stream.parse_range_header(None) is None
    assert mega_stream.parse_range_header("") is None
    spec = mega_stream.parse_range_header("bytes=-500")
    assert spec.suffix == 500
    assert mega_stream.bounded_range(spec, 4096) == (3596, 4095)
    assert mega_stream.bounded_range(spec, 100) == (0, 99)
    assert mega_stream.bounded_range(
        mega_stream.parse_range_header("bytes=10-"), 4096) == (10, 4095)


# --------------------------------------------------------------------------- 403 / two-use


def test_403_re_resolves_once_and_streams():
    urls = ["http://gfs1.userstorage.mega.co.nz/dl/expired",
            "http://gfs2.userstorage.mega.co.nz/dl/fresh"]
    resolver = FakeResolver(urls)
    storage = FakeStorage(CIPHERTEXT, statuses={urls[0]: 403})
    invalidated = []
    original = mega.invalidate
    mega.invalidate = lambda link: invalidated.append(link) or True
    try:
        plan = _engine(resolver, storage).plan(LINK)
        assert _collect(plan) == PLAINTEXT
    finally:
        mega.invalidate = original
    assert [call["url"] for call in storage.calls] == urls
    assert len(resolver.calls) == 2
    assert len(invalidated) == 1


def test_403_twice_stops_after_one_retry():
    urls = ["http://gfs1.userstorage.mega.co.nz/dl/expired",
            "http://gfs2.userstorage.mega.co.nz/dl/still-expired"]
    resolver = FakeResolver(urls)
    storage = FakeStorage(CIPHERTEXT, statuses={urls[0]: 403, urls[1]: 403})
    with pytest.raises(mega_stream.MegaStreamError) as caught:
        _engine(resolver, storage).plan(LINK)
    assert caught.value.code == 403
    assert len(resolver.calls) == 2
    assert len(storage.calls) == 2


def test_two_use_storage_capability_re_resolves_on_the_third_play():
    # The resolver's cache returns the same (stale) URL until invalidated;
    # the third resolve in this test is the one after the 403 invalidation.
    urls = ["http://gfs1.userstorage.mega.co.nz/dl/two-use",
            "http://gfs1.userstorage.mega.co.nz/dl/two-use",
            "http://gfs1.userstorage.mega.co.nz/dl/two-use",
            "http://gfs2.userstorage.mega.co.nz/dl/fresh"]
    resolver = FakeResolver(urls)
    # The first URL is a two-use capability: two opens work, the third 403s.
    storage = FakeStorage(CIPHERTEXT, uses_per_url=2)
    streamer = _engine(resolver, storage)

    assert _collect(streamer.plan(LINK)) == PLAINTEXT
    assert _collect(streamer.plan(LINK)) == PLAINTEXT
    # Third play hits the stale cached URL, then re-resolves exactly once.
    assert _collect(streamer.plan(LINK)) == PLAINTEXT

    assert len(resolver.calls) == 4
    assert [call["url"] for call in storage.calls] == [
        urls[0], urls[0], urls[0], urls[3]]


# --------------------------------------------------------------------------- head / metadata


def test_head_does_not_consume_the_storage_capability():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/head"])
    storage = FakeStorage(CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK, head=True)
    assert plan.body is None
    assert storage.calls == []
    assert plan.status == 200
    assert plan.headers["Content-Length"] == "4096"
    assert plan.headers["Accept-Ranges"] == "bytes"


def test_head_with_range_reports_partial_metadata():
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/head"])
    storage = FakeStorage(CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK, "bytes=100-199", head=True)
    assert plan.status == 206
    assert plan.headers["Content-Length"] == "100"
    assert plan.headers["Content-Range"] == "bytes 100-199/4096"
    assert storage.calls == []


@pytest.mark.parametrize("name,expected", [
    ("set.mp3", "audio/mpeg"),
    ("set.MP3", "audio/mpeg"),
    ("set.m4a", "audio/mp4"),
    ("set.wav", "audio/wav"),
    ("set.flac", "audio/flac"),
    ("set.bin", "application/octet-stream"),
    (None, "application/octet-stream"),
])
def test_content_type_from_name(name, expected):
    assert mega_stream.content_type_for(name) == expected


# --------------------------------------------------------------------------- tokens


def test_signed_token_round_trip_and_binding():
    expiry = 1000 + 60
    token = mega_stream.make_token(LINK, expiry, secret=SECRET)
    assert mega_stream.verify_token(LINK, expiry, token, secret=SECRET, now=1000)
    assert not mega_stream.verify_token(
        OTHER_LINK, expiry, token, secret=SECRET, now=1000)
    assert not mega_stream.verify_token(
        LINK, expiry, token + "x", secret=SECRET, now=1000)
    assert not mega_stream.verify_token(
        LINK, expiry, token, secret="different", now=1000)


def test_signed_token_expiry_and_far_future_are_rejected():
    now = 1_700_000_000
    expired = mega_stream.make_token(LINK, now - 1, secret=SECRET)
    assert not mega_stream.verify_token(LINK, now - 1, expired,
                                        secret=SECRET, now=now)
    far = now + mega_stream.MAX_TOKEN_TTL + 1
    token = mega_stream.make_token(LINK, far, secret=SECRET)
    assert not mega_stream.verify_token(LINK, far, token, secret=SECRET, now=now)


def test_signed_path_encodes_the_fragment_key():
    path = mega_stream.signed_path(LINK, ttl=60, now=1000, secret=SECRET)
    assert path.startswith("/api/mega/stream?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert query["u"] == [mega.parse_link(LINK).canonical]
    assert mega_stream.verify_token(
        query["u"][0], query["e"][0], query["t"][0], secret=SECRET, now=1000)


# --------------------------------------------------------------------------- Flask API


def _patch_stream(monkeypatch, resolver, storage):
    monkeypatch.setattr(mega_stream.mega, "resolve", resolver)
    monkeypatch.setattr(mega_stream, "open_storage", storage)


def test_route_streams_plaintext_with_session(web, monkeypatch):
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)
    response = web.get("/api/mega/stream", query_string={"u": LINK})
    assert response.status_code == 200
    assert response.data == PLAINTEXT
    assert response.headers["Content-Type"] == "audio/mpeg"
    assert response.headers["Content-Length"] == "4096"
    assert response.headers["Accept-Ranges"] == "bytes"


def test_route_range_request(web, monkeypatch):
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)
    response = web.get("/api/mega/stream", query_string={"u": LINK},
                       headers={"Range": "bytes=1001-1010"})
    assert response.status_code == 206
    assert response.data == PLAINTEXT[1001:1011]
    assert response.headers["Content-Range"] == "bytes 1001-1010/4096"
    assert storage.calls[0]["headers"]["Range"] == "bytes=992-1010"


def test_route_head(web, monkeypatch):
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)
    response = web.head("/api/mega/stream", query_string={"u": LINK})
    assert response.status_code == 200
    assert response.headers["Content-Length"] == "4096"
    assert storage.calls == []


def test_route_requires_auth_without_token(anon, monkeypatch):
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)
    response = anon.get("/api/mega/stream", query_string={"u": LINK})
    assert response.status_code == 401
    assert resolver.calls == []
    assert storage.calls == []


def test_route_accepts_a_signed_token_without_a_cookie(anon, monkeypatch):
    monkeypatch.setenv("RAVEWEB_MEGA_TOKEN_SECRET", SECRET)
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)
    path = mega_stream.signed_path(LINK, secret=SECRET)
    response = anon.get(path)
    assert response.status_code == 200
    assert response.data == PLAINTEXT


def test_route_rejects_tampered_and_expired_tokens(anon, monkeypatch):
    monkeypatch.setenv("RAVEWEB_MEGA_TOKEN_SECRET", SECRET)
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)

    path = mega_stream.signed_path(LINK, secret=SECRET)
    tampered = path[:-1] + ("A" if path[-1] != "A" else "B")
    assert anon.get(tampered).status_code == 401

    expired = mega_stream.signed_path(
        LINK, ttl=1, now=time.time() - 10, secret=SECRET)
    assert anon.get(expired).status_code == 401
    assert storage.calls == []


def test_route_rejects_non_mega_links(web, monkeypatch):
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/web"])
    storage = FakeStorage(CIPHERTEXT)
    _patch_stream(monkeypatch, resolver, storage)
    response = web.get("/api/mega/stream",
                       query_string={"u": "https://example.com/a.mp3"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "unsupported"
    assert resolver.calls == []
    assert storage.calls == []


def test_route_rejects_missing_link(web):
    response = web.get("/api/mega/stream")
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid"


def test_sign_endpoint_returns_a_token_path(web, monkeypatch):
    monkeypatch.setenv("RAVEWEB_MEGA_TOKEN_SECRET", SECRET)
    response = web.get("/api/mega/sign", query_string={"u": LINK})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["path"].startswith("/api/mega/stream?")
    assert payload["absolute_url"].endswith(payload["path"])
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(payload["path"]).query)
    assert mega_stream.has_valid_token({key: value[0] for key, value in query.items()})


def test_sign_endpoint_requires_auth_and_a_mega_link(anon, web):
    assert anon.get("/api/mega/sign", query_string={"u": LINK}).status_code == 401
    assert web.get("/api/mega/sign",
                   query_string={"u": "https://example.com/a.mp3"}).status_code == 400


def test_resolve_response_carries_a_stream_path_for_mega(monkeypatch):
    monkeypatch.setenv("RAVEWEB_MEGA_TOKEN_SECRET", SECRET)
    resolved = Resolved("http://gfs1.userstorage.mega.co.nz/dl/x", {}, 5000,
                        "mega")
    with app_module.app.test_request_context("/api/resolve"):
        response = resolver_service._success_response(resolved, LINK)
        payload = response.get_json()
    assert payload["stream"].startswith("/api/mega/stream?")
    # A non-MEGA source never gets the extra field.
    with app_module.app.test_request_context("/api/resolve"):
        plain = resolver_service._success_response(
            Resolved("https://cdn.example.com/a.mp3", {}, 5000, "direct"),
            "https://cdn.example.com/a.mp3").get_json()
    assert "stream" not in plain


# --------------------------------------------------------------------------- logging


def test_stream_bytes_and_keys_are_never_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="raveweb.mega_stream")
    resolver = FakeResolver(["http://gfs1.userstorage.mega.co.nz/dl/secret-token"])
    storage = FakeStorage(CIPHERTEXT)
    plan = _engine(resolver, storage).plan(LINK)
    assert _collect(plan) == PLAINTEXT
    text = caplog.text
    assert "secret-token" not in text
    assert KEY_B64 not in text
    assert "MEGA stream known fixture" not in text
    assert PLAINTEXT[:16].decode("latin-1") not in text
