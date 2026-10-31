# -*- coding: utf-8 -*-
"""SSRF guard and stream proxy behaviour, with DNS and sockets faked."""

import socket

import pytest


PUBLIC = "93.184.216.34"


class FakeSock:
    def __init__(self):
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None, read_error=None):
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.chunks = list(chunks or [])
        self.read_error = read_error
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, amount):
        if self.chunks:
            return self.chunks.pop(0)
        if self.read_error is not None:
            raise self.read_error
        return b""

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = FakeSock()
        self.method = None
        self.target = None
        self.headers = None
        self.closed = False
        self.pin = None

    def request(self, method, target, headers=None):
        self.method = method
        self.target = target
        self.headers = dict(headers or {})

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture()
def upstream(monkeypatch, dns):
    """Install fake connections; returns (build, connections)."""
    connections = []

    def build(responses):
        queue = list(responses)

        def fake_new(scheme, host, port, ip):
            response = queue.pop(0) if len(queue) > 1 else queue[0]
            connection = FakeConnection(response)
            connection.pin = (scheme, host, port, ip)
            connections.append(connection)
            return connection

        monkeypatch.setattr("app._new_connection", fake_new)
        return connections

    return build


def stream(web, url, **kwargs):
    headers = kwargs.pop("headers", None)
    extra = kwargs.pop("query", None)
    query = {"u": url}
    if extra:
        query.update(extra)
    return web.get("/api/stream", query_string=query, headers=headers)


# --------------------------------------------------------------------------- SSRF rules


@pytest.mark.parametrize("url", [
    "",
    "file:///etc/passwd",
    "ftp://example.com/a.mp3",
    "gopher://example.com/",
    "javascript:alert(1)",
    "data:audio/mpeg;base64,AAAA",
    "https://",
    "https://user:pass@example.com/a.mp3",
    "http://example.com:22/a.mp3",
    "http://example.com:0/a.mp3",
    "http://example.com:99999/a.mp3",
    "http://example.com/\r\nX-Evil: 1",
])
def test_bad_urls_are_refused(web, dns, url):
    response = stream(web, url)
    assert response.status_code == 400
    assert response.get_json() == {"error": "blocked or missing url"}
    assert dns["calls"] == []


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.5.4", "192.168.10.10", "169.254.1.1",
    "0.0.0.0", "100.64.0.1", "::1", "fd00::1", "fe80::1", "::ffff:127.0.0.1",
])
def test_private_dns_answers_are_refused(web, dns, address):
    import app as app_module

    dns["addresses"] = [address]
    assert app_module._host_allowed("http://evil.example/a.mp3") is False


def test_mixed_public_private_answers_are_refused(web, dns):
    import app as app_module

    dns["addresses"] = [PUBLIC, "10.0.0.5"]
    assert app_module._host_allowed("https://rebind.example/a.mp3") is False


def test_dns_failure_is_refused(web, dns):
    import app as app_module

    dns["error"] = socket.gaierror("no such host")
    assert app_module._host_allowed("https://nope.example/a.mp3") is False


def test_public_host_is_allowed(dns):
    import app as app_module

    assert app_module._host_allowed("https://example.com/a.mp3") is True


def test_public_ipv6_literal_is_allowed(dns):
    import app as app_module

    assert app_module._host_allowed("http://[2606:4700:4700::1111]/a.mp3") is True
    assert dns["calls"] == []


def test_private_ipv6_literal_is_refused(dns):
    import app as app_module

    assert app_module._host_allowed("http://[::1]:8000/a.mp3") is False


def test_idn_host_is_encoded(dns):
    import app as app_module

    assert app_module._host_allowed("https://caf\u00e9.example/a.mp3") is True
    assert dns["calls"][-1][0] == "xn--caf-dma.example"


def test_private_allowed_when_opted_in(web, dns, upstream):
    import app as app_module

    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"},
                                         chunks=[b"abc"])])
    app_module.ALLOW_PRIVATE = True
    try:
        response = stream(web, "http://127.0.0.1:8098/radio.mp3")
        assert response.status_code == 200
        assert connections[0].pin[3] == "127.0.0.1"
    finally:
        app_module.ALLOW_PRIVATE = False


def test_dns_answer_is_pinned_for_the_connection(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"},
                                         chunks=[b"abc"])])
    response = stream(web, "https://example.com/set.mp3")
    assert response.status_code == 200
    scheme, host, port, ip = connections[0].pin
    assert (scheme, host, port, ip) == ("https", "example.com", 443, PUBLIC)
    assert connections[0].target == "/set.mp3"


# --------------------------------------------------------------------------- happy path


def test_range_request_is_forwarded(web, upstream):
    connections = upstream([FakeResponse(
        status=206,
        headers={"Content-Type": "audio/mpeg", "Content-Length": "4",
                 "Content-Range": "bytes 0-3/996", "Accept-Ranges": "bytes"},
        chunks=[b"abcd"],
    )])
    response = stream(web, "https://archive.org/download/x/a.mp3",
                      headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content_type == "audio/mpeg"
    assert response.headers["Content-Range"] == "bytes 0-3/996"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.data == b"abcd"
    assert connections[0].headers["Range"] == "bytes=0-3"
    assert connections[0].headers["Accept-Encoding"] == "identity"


def test_no_range_means_no_range_header(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"},
                                         chunks=[b"x"])])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 200
    assert "Range" not in connections[0].headers


def test_multiple_ranges_are_not_forwarded(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"},
                                         chunks=[b"x"])])
    response = stream(web, "https://example.com/a.mp3",
                      headers={"Range": "bytes=0-10, 20-30"})
    assert response.status_code == 200
    assert "Range" not in connections[0].headers


def test_referer_defaults_to_target_host(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"})])
    stream(web, "https://example.com/a.mp3")
    assert connections[0].headers["Referer"] == "https://example.com/"


def test_referer_header_injection_is_dropped(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"})])
    response = stream(web, "https://example.com/a.mp3",
                      query={"ref": "http://evil/\r\nX-Injected: yes"})
    assert response.status_code == 200
    assert "X-Injected" not in connections[0].headers
    assert connections[0].headers["Referer"] == "https://example.com/"


def test_kodi_user_agent_injection_is_dropped(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"})])
    url = "https://example.com/a.mp3|Referer=https://example.com/&User-Agent=evil\r\nX-Bad: 1"
    response = stream(web, url)
    assert response.status_code == 200
    assert "X-Bad" not in connections[0].headers
    assert connections[0].headers["User-Agent"] == "RavePlayer-Web/1.0 (+https://github.com/tcz009/plugin.audio.raveplayer)"


def test_kodi_suffix_becomes_headers(web, upstream):
    connections = upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"})])
    url = ("https://www.ravetapepacks.com/music/E/Bradley_M.mp3"
           "|Referer=https://www.ravetapepacks.com/&User-Agent=Mozilla/5.0")
    response = stream(web, url)
    assert response.status_code == 200
    assert connections[0].target == "/music/E/Bradley_M.mp3"
    assert connections[0].headers["Referer"] == "https://www.ravetapepacks.com/"
    assert connections[0].headers["User-Agent"] == "Mozilla/5.0"


# --------------------------------------------------------------------------- redirects


def test_public_redirect_is_followed(web, upstream):
    connections = upstream([
        FakeResponse(status=302, headers={"Location": "https://cdn.example/b.mp3"}),
        FakeResponse(status=206, headers={"Content-Type": "audio/mpeg",
                                          "Content-Range": "bytes 0-1/99"},
                     chunks=[b"xy"]),
    ])
    response = stream(web, "https://archive.org/download/x/a.mp3")
    assert response.status_code == 206
    assert response.data == b"xy"
    assert len(connections) == 2
    assert connections[0].closed is True
    assert connections[1].pin[1] == "cdn.example"
    assert connections[1].target == "/b.mp3"


def test_redirect_to_private_address_is_refused(web, upstream):
    connections = upstream([
        FakeResponse(status=302, headers={"Location": "http://127.0.0.1:8098/healthz"}),
    ])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502
    assert response.get_json()["error"] == "upstream connection failed"
    assert len(connections) == 1


def test_redirect_to_file_scheme_is_refused(web, upstream):
    upstream([FakeResponse(status=302, headers={"Location": "file:///etc/passwd"})])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502


def test_redirect_loop_is_cut_off(web, upstream):
    loop = [FakeResponse(status=302, headers={"Location": "https://example.com/a.mp3"})
            for _ in range(6)]
    upstream(loop)
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502


# --------------------------------------------------------------------------- upstream failures


def test_upstream_404_maps_to_404(web, upstream):
    upstream([FakeResponse(status=404, headers={"Content-Type": "text/html"})])
    response = stream(web, "https://example.com/missing.mp3")
    assert response.status_code == 404
    assert response.get_json()["error"] == "upstream media not found"


def test_upstream_403_maps_to_403(web, upstream):
    upstream([FakeResponse(status=403, headers={"Content-Type": "text/html"})])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 403
    assert response.get_json()["error"] == "upstream refused access"


def test_upstream_500_maps_to_502(web, upstream):
    upstream([FakeResponse(status=500)])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502
    assert response.get_json()["error"] == "upstream unavailable"


def test_html_block_page_is_not_served_as_audio(web, upstream):
    upstream([FakeResponse(status=200, headers={"Content-Type": "text/html; charset=utf-8"},
                           chunks=[b"<html>hotlink denied</html>"])])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502
    assert response.get_json()["error"] == "upstream did not return audio"


def test_gzip_stream_is_refused(web, upstream):
    upstream([FakeResponse(status=200,
                           headers={"Content-Type": "audio/mpeg", "Content-Encoding": "gzip"})])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502


def test_416_is_passed_through_with_content_range(web, upstream):
    upstream([FakeResponse(status=416, headers={
        "Content-Type": "text/html", "Content-Range": "bytes */996"})])
    response = stream(web, "https://example.com/a.mp3",
                      headers={"Range": "bytes=2000-3000"})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */996"


def test_connect_timeout_maps_to_504(web, upstream, monkeypatch):
    def slow(*args, **kwargs):
        raise socket.timeout("timed out")

    upstream([FakeResponse()])
    monkeypatch.setattr(FakeConnection, "getresponse", slow)
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 504
    assert response.get_json()["error"] == "upstream timed out"


def test_read_timeout_truncates_the_body(web, upstream):
    upstream([FakeResponse(status=200, headers={"Content-Type": "audio/mpeg"},
                           chunks=[b"abc"], read_error=socket.timeout("stalled"))])
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 200
    assert response.data == b"abc"


def test_unknown_exception_does_not_leak_a_traceback(web, upstream, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("internal detail")

    upstream([FakeResponse()])
    monkeypatch.setattr(FakeConnection, "request", explode)
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 502
    assert "internal detail" not in response.get_data(as_text=True)


# --------------------------------------------------------------------------- limits


class BusySlots:
    def acquire(self, timeout=None):
        return False

    def release(self):  # pragma: no cover - must never be called
        raise AssertionError("released an unacquired slot")


def test_stream_limit_returns_503(web, upstream, monkeypatch):
    upstream([FakeResponse()])
    monkeypatch.setattr("app._STREAM_SLOTS", BusySlots())
    response = stream(web, "https://example.com/a.mp3")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def test_stream_slot_is_released_after_success(web, upstream):
    import app as app_module

    upstream([FakeResponse(headers={"Content-Type": "audio/mpeg"}, chunks=[b"x"])])
    for _ in range(app_module.MAX_STREAMS + 1):
        assert stream(web, "https://example.com/a.mp3").status_code == 200
