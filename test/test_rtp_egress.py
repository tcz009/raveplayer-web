# -*- coding: utf-8 -*-
"""Per-host RaveTapePacks egress: only RTP hosts take the proxy.

Everything here is offline: a local mock proxy and a local target server
stand in for the VPN host and for the rest of the internet.
"""

import http.server
import socket
import threading
import urllib.request

import pytest



class _Recorder(object):
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def add(self, handler, body):
        with self.lock:
            self.requests.append({
                "path": handler.path,
                "headers": {key.lower(): value for key, value in handler.headers.items()},
                "method": handler.command,
            })

    def clear(self):
        with self.lock:
            self.requests = []


def _serve(handler_class):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture()
def proxy(monkeypatch):
    """Local HTTP proxy that records what reached it and answers 200."""
    recorder = _Recorder()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            recorder.add(self, b"")
            if "redirect-private" in self.path:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/private")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"proxy-body"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_CONNECT = do_GET

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    url = "http://127.0.0.1:%d" % server.server_address[1]
    monkeypatch.setenv("RTP_EGRESS_PROXY", url)
    yield {"url": url, "recorder": recorder}
    server.shutdown()
    server.server_close()


@pytest.fixture()
def target():
    """Local origin server for the direct path (stands in for other hosts)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"direct-body"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _no_polite_gap(monkeypatch):
    from core import fetch as core_fetch

    monkeypatch.setattr(core_fetch, "POLITE_HOSTS", {})


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_proxy_selected_only_for_rtp_hosts(proxy, target):
    from core import fetch as core_fetch

    body = core_fetch.fetch("http://www.ravetapepacks.com/page.html")
    assert body == "proxy-body"
    assert len(proxy["recorder"].requests) == 1
    assert proxy["recorder"].requests[0]["path"].startswith(
        "http://www.ravetapepacks.com/")

    proxy["recorder"].clear()
    body = core_fetch.fetch(target + "/other.mp3")
    assert body == "direct-body"
    assert proxy["recorder"].requests == []


def test_browser_headers_and_referer_survive_the_proxy(proxy, target):
    from core import fetch as core_fetch

    body = core_fetch.fetch(
        "http://www.ravetapepacks.com/",
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0",
                 "Referer": "https://www.ravetapepacks.com/",
                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
    )
    assert body == "proxy-body"
    sent = proxy["recorder"].requests[0]["headers"]
    assert sent["user-agent"] == "Mozilla/5.0"
    assert sent["referer"] == "https://www.ravetapepacks.com/"
    assert "text/html" in sent["accept"]


def test_proxy_down_falls_back_to_direct(proxy, target, monkeypatch):
    from core import fetch as core_fetch

    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://127.0.0.1:%d" % _closed_port())
    monkeypatch.setenv("RTP_EGRESS_HOSTS", "127.0.0.1")
    body = core_fetch.fetch(target + "/page.html")
    assert body == "direct-body"
    assert proxy["recorder"].requests == []


def test_fallback_can_be_disabled(proxy, target, monkeypatch):
    from core import fetch as core_fetch

    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://127.0.0.1:%d" % _closed_port())
    monkeypatch.setenv("RTP_EGRESS_HOSTS", "127.0.0.1")
    monkeypatch.setenv("RTP_EGRESS_FALLBACK", "0")
    assert core_fetch.fetch(target + "/page.html") is None


def test_redirect_to_private_is_still_refused_with_proxy(proxy):
    from core import fetch as core_fetch

    assert core_fetch.fetch(
        "http://www.ravetapepacks.com/redirect-private") is None
    assert len(proxy["recorder"].requests) == 1


def test_https_rtp_request_is_tunnelled(monkeypatch):
    from core import fetch as core_fetch

    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://127.0.0.1:8888")
    handler = core_fetch._EgressProxyHandler({
        "http": "http://127.0.0.1:8888",
        "https": "http://127.0.0.1:8888",
    })

    request = urllib.request.Request("https://www.ravetapepacks.com/music/x/a.mp3")
    handler.proxy_open(request, "http://127.0.0.1:8888", "https")
    assert request.host == "127.0.0.1:8888"
    assert request._tunnel_host == "www.ravetapepacks.com"


def test_non_rtp_https_request_is_not_proxied(monkeypatch):
    from core import fetch as core_fetch

    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://127.0.0.1:8888")
    handler = core_fetch._EgressProxyHandler({
        "http": "http://127.0.0.1:8888",
        "https": "http://127.0.0.1:8888",
    })

    request = urllib.request.Request("https://archive.org/download/a.mp3")
    assert handler.proxy_open(request, "http://127.0.0.1:8888", "https") is None
    assert request.host == "archive.org"


def test_egress_host_boundaries_and_config(monkeypatch):
    from core import fetch as core_fetch

    monkeypatch.delenv("RTP_EGRESS_HOSTS", raising=False)
    assert core_fetch.is_egress_host("www.ravetapepacks.com") is True
    assert core_fetch.is_egress_host("ravetapepacks.com") is True
    assert core_fetch.is_egress_host("https://www.ravetapepacks.com/x") is True
    assert core_fetch.is_egress_host("notravetapepacks.com") is False
    assert core_fetch.is_egress_host("example.com") is False

    monkeypatch.setenv("RTP_EGRESS_PROXY", "192.0.2.10:8888")
    assert core_fetch.egress_proxy() == "http://192.0.2.10:8888"
    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://user:pass@proxy.example:3128")
    assert core_fetch.egress_proxy() == "http://user:pass@proxy.example:3128"
    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://proxy.example:3128/path")
    assert core_fetch.egress_proxy() == ""


# --------------------------------------------------------------------- /api/stream


class _FakeSock(object):
    def settimeout(self, value):
        pass

    def close(self):
        pass


class _FakeResponse(object):
    def __init__(self, status=200, chunks=None):
        self.status = status
        self.headers = {"content-type": "audio/mpeg"}
        self.chunks = list(chunks or [b"ID3"])
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, amount):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class _FakeConnection(object):
    def __init__(self, response):
        self.response = response
        self.sock = _FakeSock()
        self.request_args = None
        self.closed = False

    def request(self, method, target, headers=None):
        self.request_args = (method, target, dict(headers or {}))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_stream_uses_proxy_for_rtp_only(web, dns, monkeypatch):
    import app as app_module

    calls = {"proxy": [], "direct": []}

    def fake_proxy(scheme, host, port, proxy):
        calls["proxy"].append((scheme, host, port, proxy))
        return _FakeConnection(_FakeResponse()), {}

    def fake_direct(*args):
        calls["direct"].append(args)
        return _FakeConnection(_FakeResponse())

    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setattr(app_module, "_new_proxy_connection", fake_proxy)
    monkeypatch.setattr(app_module, "_new_connection", fake_direct)

    response = web.get("/api/stream", query_string={
        "u": "https://www.ravetapepacks.com/music/Pack/a.mp3",
        "ref": "https://www.ravetapepacks.com/",
    })
    assert response.status_code == 200
    assert response.data == b"ID3"
    assert calls["direct"] == []
    assert calls["proxy"][0][:3] == ("https", "www.ravetapepacks.com", 443)

    calls["proxy"].clear()
    response = web.get("/api/stream", query_string={
        "u": "https://archive.org/download/x/a.mp3"})
    assert response.status_code == 200
    assert calls["proxy"] == []
    assert len(calls["direct"]) == 1


def test_stream_falls_back_direct_when_proxy_errors(web, dns, monkeypatch):
    import app as app_module

    calls = {"direct": 0}

    def broken_proxy(*args):
        raise ConnectionRefusedError("proxy down")

    def fake_direct(*args):
        calls["direct"] += 1
        return _FakeConnection(_FakeResponse())

    monkeypatch.setenv("RTP_EGRESS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setattr(app_module, "_new_proxy_connection", broken_proxy)
    monkeypatch.setattr(app_module, "_new_connection", fake_direct)

    response = web.get("/api/stream", query_string={
        "u": "https://www.ravetapepacks.com/music/Pack/a.mp3"})
    assert response.status_code == 200
    assert calls["direct"] == 1
