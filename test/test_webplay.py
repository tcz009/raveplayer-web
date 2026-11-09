# -*- coding: utf-8 -*-
"""Signed playback URLs for the web-proxied Mediafire streams.

Kodi can read the source lists with ``RAVEWEB_RESOLVER_TOKEN`` but cannot put a
bearer on the URL an ``<audio>`` player is handed and has no browser cookie.
``/api/webplay`` exchanges that bearer for a short-lived HMAC-signed URL bound
to one ``/api/(ezgarage|ukraves)/<id>/stream`` path.

Everything is offline: the Mediafire resolver and the stream proxy's connection
layer are fakes.
"""

import hashlib
import hmac
import logging
import time
import urllib.parse

import pytest

import app as app_module
from core import webplay

TOKEN = "kodi-service-token"
EZ_ID = "72aswcrx4hlw2sh"
UK_ID = "7524"
EZ_PATH = "/api/ezgarage/%s/stream" % EZ_ID
UK_PATH = "/api/ukraves/%s/stream" % UK_ID
SIGNED = (
    "https://download1346.mediafire.com/40kvzr3mwokgqHEiZDXv/72aswcrx4hlw2sh/"
    "DJ+EZ%40LaCosaNostra.mp3?token=abc123&expires=1799999999"
)


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setattr(app_module, "RESOLVER_SERVICE_TOKEN", TOKEN)


def _headers(token=TOKEN):
    return {"Authorization": "Bearer %s" % token}


def _mint(client, path, token=TOKEN):
    return client.get("/api/webplay", query_string={"path": path},
                      headers=_headers(token))


def _split(url):
    parts = urllib.parse.urlsplit(url)
    return parts.path, urllib.parse.parse_qs(parts.query)


def _query(path, args):
    flat = {key: value[0] if isinstance(value, (list, tuple)) else value
            for key, value in args.items()}
    return path + "?" + urllib.parse.urlencode(flat)


# --------------------------------------------------------------------------- fake upstream


class _FakeResponse(object):
    def __init__(self, body=b"ID3" + b"a" * 40, status=200,
                 content_type="audio/mpeg"):
        self.status = status
        self._body = body
        self._offset = 0
        self._content_type = content_type

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return self._content_type
        if lowered == "content-length":
            return str(len(self._body))
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
        calls.append((url, dict(headers)))
        return _FakeConnection(), _FakeResponse(), url

    monkeypatch.setattr(app_module, "_open_stream", fake_open_stream)
    return calls


def _patch_ezgarage(monkeypatch):
    monkeypatch.setattr(app_module.ezgarage, "resolve", lambda set_id: SIGNED)


def _patch_ukraves(monkeypatch):
    monkeypatch.setattr(app_module.ukraves, "mediafire_url",
                        lambda mix_id: SIGNED)


# --------------------------------------------------------------------------- signing


def test_signature_is_hmac_sha256_over_path_nul_expiry():
    expiry = 1_800_000_000
    expected = hmac.new(
        str(app_module.app.secret_key).encode("utf-8"),
        EZ_PATH.encode("utf-8") + b"\x00" + str(expiry).encode("ascii"),
        hashlib.sha256).hexdigest()
    assert webplay.signature(EZ_PATH, expiry) == expected
    assert webplay.verify(EZ_PATH, str(expiry), expected,
                          now=expiry - 60)


def test_signed_path_is_the_documented_shape_and_default_ttl_is_one_hour():
    now = 1_800_000_000
    signed = webplay.signed_path(EZ_PATH, now=now)
    path, query = _split(signed)
    assert path == EZ_PATH
    assert int(query["e"][0]) == now + 3600
    assert query["t"][0] == webplay.signature(EZ_PATH, now + 3600)
    assert signed.startswith(EZ_PATH + "?e=")


@pytest.mark.parametrize("path", [
    EZ_PATH,
    UK_PATH,
    "/api/ezgarage/abc123/stream",
    "/api/ukraves/1/stream",
    "/api/ezgarage/a_b-c/stream",
])
def test_only_the_two_exact_stream_shapes_are_signable(path):
    assert webplay.is_signable_path(path)


@pytest.mark.parametrize("path", [
    "",
    "api/ezgarage/%s/stream" % EZ_ID,
    "https://example.com" + EZ_PATH,
    "//example.com" + EZ_PATH,
    EZ_PATH + "/",
    EZ_PATH + "?x=1",
    EZ_PATH + "#frag",
    EZ_PATH + "\n",
    EZ_PATH.replace("/stream", "/file"),
    "/api/ukraves/%s/stream/" % UK_ID,
    "/api/ukraves/%s/file" % UK_ID,
    "/api/latest",
    "/api/mega/stream",
    "/api/ezgarage/../stream",
    "/api/ezgarage/%s/stream/extra" % EZ_ID,
    "/api/ezgarage/%s/Stream" % EZ_ID,
    "/api/podcast/%s/stream" % EZ_ID,
])
def test_other_paths_are_not_signable(path):
    assert not webplay.is_signable_path(path)
    with pytest.raises(webplay.WebplayError):
        webplay.signed_path(path)


# --------------------------------------------------------------------------- /api/webplay auth


def test_webplay_requires_the_bearer(anon):
    assert anon.get("/api/webplay", query_string={"path": EZ_PATH}).status_code == 401
    assert anon.get("/api/webplay", query_string={"path": EZ_PATH},
                    headers=_headers("wrong")).status_code == 401
    # The strict Bearer form is required here; the X-Resolve-Token alias is not.
    assert anon.get("/api/webplay", query_string={"path": EZ_PATH},
                    headers={"X-Resolve-Token": TOKEN}).status_code == 401


def test_webplay_still_requires_the_bearer_with_a_session(web):
    assert web.get("/api/webplay",
                   query_string={"path": EZ_PATH}).status_code == 401


def test_webplay_is_off_when_no_service_token_is_configured(anon, monkeypatch):
    monkeypatch.setattr(app_module, "RESOLVER_SERVICE_TOKEN", "")
    response = _mint(anon, EZ_PATH)
    assert response.status_code == 401


def test_webplay_is_in_the_scoped_service_token_get_allowlist():
    assert "/api/webplay" in app_module.RESOLVER_TOKEN_GET_PATHS
    assert app_module._resolver_token_path_allowed("/api/webplay", "GET")
    assert app_module._resolver_token_path_allowed("/api/webplay", "HEAD")
    assert not app_module._resolver_token_path_allowed("/api/webplay", "POST")


@pytest.mark.parametrize("path", [
    "",
    "https://example.com" + EZ_PATH,
    "//example.com" + EZ_PATH,
    EZ_PATH + "/",
    EZ_PATH + "?x=1",
    EZ_PATH.replace("/stream", "/file"),
    UK_PATH.replace("/stream", "/file"),
    "/api/latest",
    "/api/mega/stream",
    "/api/ezgarage/../stream",
    "/api/ezgarage/%s/stream/extra" % EZ_ID,
])
def test_webplay_rejects_absolute_and_other_paths(anon, path):
    response = _mint(anon, path)
    assert response.status_code == 400, (path, response.data)
    assert response.get_json()["code"] == "invalid_path"


def test_webplay_requires_a_path(anon):
    assert anon.get("/api/webplay", headers=_headers()).status_code == 400


def test_webplay_methods_are_get_and_head_only(anon):
    get = _mint(anon, EZ_PATH)
    assert get.status_code == 200
    head = anon.head("/api/webplay",
                     query_string={"path": EZ_PATH}, headers=_headers())
    assert head.status_code == 200
    assert head.data == b""
    for method in ("post", "put", "patch", "delete"):
        response = getattr(anon, method)("/api/webplay",
                                         query_string={"path": EZ_PATH},
                                         headers=_headers())
        assert response.status_code == 405, (method, response.data)
        assert response.headers["Allow"] == "GET, HEAD"


# --------------------------------------------------------------------------- mint + stream


@pytest.mark.parametrize("path,patch", [
    (EZ_PATH, _patch_ezgarage),
    (UK_PATH, _patch_ukraves),
])
def test_webplay_mints_an_absolute_signed_url_that_streams_without_a_cookie(
        anon, monkeypatch, path, patch):
    patch(monkeypatch)
    calls = _patch_stream(monkeypatch)

    response = _mint(anon, path)
    assert response.status_code == 200, response.data
    payload = response.get_json()
    assert set(payload) == {"ok", "url"}
    assert payload["ok"] is True
    url = payload["url"]
    assert url.startswith("http://localhost" + path + "?")

    signed_path, query = _split(url)
    assert signed_path == path
    assert set(query) == {"e", "t"}
    expiry = int(query["e"][0])
    assert 3590 <= expiry - int(time.time()) <= 3610
    expected = hmac.new(
        str(app_module.app.secret_key).encode("utf-8"),
        path.encode("utf-8") + b"\x00" + query["e"][0].encode("ascii"),
        hashlib.sha256).hexdigest()
    assert query["t"][0] == expected
    assert response.headers["Cache-Control"] == "no-store"

    played = anon.get(_query(signed_path, {"e": query["e"][0],
                                          "t": query["t"][0]}))
    assert played.status_code == 200, played.data
    assert played.data.startswith(b"ID3")
    assert played.headers["Content-Type"].startswith("audio/")
    assert calls and calls[0][0] == SIGNED


def test_stream_without_a_signature_is_still_session_only(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    _patch_ukraves(monkeypatch)
    calls = _patch_stream(monkeypatch)
    assert anon.get(EZ_PATH).status_code == 401
    assert anon.get(UK_PATH).status_code == 401
    assert calls == []


def test_signed_stream_paths_accept_only_get_and_head(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    calls = _patch_stream(monkeypatch)
    query = _split(_mint(anon, EZ_PATH).get_json()["url"])[1]

    head = anon.head(_query(EZ_PATH, query))
    try:
        assert head.status_code == 200
        assert head.data == b""
        assert calls
    finally:
        # A streamed response holds a request context; close it before the
        # test client starts the next request.
        head.close()

    # The signature is a GET/HEAD playback capability, never a mutation grant.
    assert anon.post(_query(EZ_PATH, query)).status_code == 401


# --------------------------------------------------------------------------- rejection


def test_tampered_expiry_and_token_are_rejected(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    calls = _patch_stream(monkeypatch)
    _path, query = _split(_mint(anon, EZ_PATH).get_json()["url"])

    tampered = "0" if query["t"][0][-1] != "0" else "1"
    token = query["t"][0][:-1] + tampered
    assert anon.get(_query(EZ_PATH, {"e": query["e"][0],
                                     "t": token})).status_code == 401

    bumped = str(int(query["e"][0]) + 1)
    assert anon.get(_query(EZ_PATH, {"e": bumped,
                                     "t": query["t"][0]})).status_code == 401
    assert calls == []


def test_leading_zero_expiry_is_rejected(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    calls = _patch_stream(monkeypatch)
    _path, query = _split(_mint(anon, EZ_PATH).get_json()["url"])
    zeroed = "0" + query["e"][0]
    # Same integer, different signed spelling: the NUL-separated message does
    # not match, so this is a tampered URL rather than the same expiry.
    assert anon.get(_query(EZ_PATH, {"e": zeroed,
                                     "t": query["t"][0]})).status_code == 401
    assert calls == []


def test_cross_path_signatures_are_rejected(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    _patch_ukraves(monkeypatch)
    calls = _patch_stream(monkeypatch)
    _path, query = _split(_mint(anon, EZ_PATH).get_json()["url"])

    # Same signature on the other source and on a sibling id.
    assert anon.get(_query(UK_PATH, query)).status_code == 401
    other = "/api/ezgarage/%s/stream" % "bavkbz5zvku5284"
    assert anon.get(_query(other, query)).status_code == 401
    assert calls == []


def test_expired_signatures_are_rejected(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    calls = _patch_stream(monkeypatch)
    expired = webplay.signed_path(EZ_PATH, ttl=1, now=time.time() - 10)
    _path, query = _split(expired)
    assert anon.get(_query(EZ_PATH, query)).status_code == 401
    assert calls == []


def test_missing_e_or_t_is_rejected(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    calls = _patch_stream(monkeypatch)
    _path, query = _split(_mint(anon, EZ_PATH).get_json()["url"])
    assert anon.get(_query(EZ_PATH, {"e": query["e"][0]})).status_code == 401
    assert anon.get(_query(EZ_PATH, {"t": query["t"][0]})).status_code == 401
    assert anon.get(_query(EZ_PATH, {"e": "not-a-number",
                                     "t": query["t"][0]})).status_code == 401
    assert calls == []


def test_signature_never_authorises_other_endpoints(anon, monkeypatch):
    _patch_ezgarage(monkeypatch)
    _patch_stream(monkeypatch)
    query = _split(_mint(anon, EZ_PATH).get_json()["url"])[1]

    for path in (
        "/api/latest",
        "/api/ezgarage/%s/file" % EZ_ID,
        "/api/ukraves/%s/file" % UK_ID,
        "/api/mega/stream",
        "/api/resolve",
        "/api/webplay",
    ):
        assert anon.get(_query(path, query)).status_code == 401, path


def test_signed_token_is_not_a_webplay_credential(anon):
    query = _split(_mint(anon, EZ_PATH).get_json()["url"])[1]
    args = {key: value[0] for key, value in query.items()}
    args["path"] = EZ_PATH
    assert anon.get("/api/webplay", query_string=args).status_code == 401


def test_app_logs_do_not_contain_the_signature(anon, monkeypatch, caplog):
    _patch_ezgarage(monkeypatch)
    _patch_stream(monkeypatch)
    caplog.set_level(logging.DEBUG, logger=app_module.app.logger.name)
    _path, query = _split(_mint(anon, EZ_PATH).get_json()["url"])
    token = query["t"][0]
    played = anon.get(_query(EZ_PATH, query))
    assert played.status_code == 200
    assert token not in caplog.text
    assert "t=" not in caplog.text
