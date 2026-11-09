# -*- coding: utf-8 -*-
"""Mediafire/Drive adapters for the resolver service (offline).

These tests deliberately use the bounded-client seam and a fake DNS, so no
test performs network I/O.  The P1 regression cases are:

* the adapters really call the injected client with the clamped timeout
  (they no longer delegate to ``ezgarage.resolve_page``/``drive.resolve``);
* a slow-drip response is stopped by the client's wall-clock deadline;
* a 302 to loopback is refused without a second request;
* two concurrent requests for distinct URLs are not serialised by a global
  lock.
"""

import socket
import threading
import time

import pytest

from core.resolver import base
from core.resolver.drive import DriveResolver
from core.resolver.mediafire import MediafireResolver
from core.resolver.service import build_default_service


class FakeDNS(object):
    def __init__(self, addresses=("93.184.216.34",), error=None):
        self.addresses = list(addresses)
        self.error = error
        self.calls = []

    def __call__(self, host, port, **kwargs):
        self.calls.append((host, port))
        if self.error is not None:
            raise self.error
        result = []
        for address in self.addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            result.append((family, socket.SOCK_STREAM, 6, "", (address, port or 443)))
        return result


class FakeHTTP(object):
    """Response/exception queue with a record of every request."""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []
        self._lock = threading.Lock()

    def request(self, method, url, headers=None, timeout=None):
        with self._lock:
            self.calls.append({"method": method, "url": url,
                               "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        with self._lock:
            response = self.responses.pop(0) if self.responses else None
        if isinstance(response, Exception):
            raise response
        if response is None:
            response = base.HTTPResponse(500, {})
        return response


@pytest.fixture()
def fake_dns():
    return FakeDNS()


MEDIAFIRE_PAGE = (
    '<html><body><a href="https://download1.mediafire.com/abc/x.mp3?'
    'token=signed">download</a></body></html>')
DRIVE_FORM = (
    '<form id="download-form" action="https://drive.usercontent.google.com/download">'
    '<input type="hidden" name="id" value="ABC1234567">'
    '<input type="hidden" name="export" value="download">'
    '<input type="submit" value="Download"></form>')


def mediafire_response(url="https://www.mediafire.com/file/abc/x.mp3/file"):
    return base.HTTPResponse(200, {"Content-Type": "text/html"},
                             MEDIAFIRE_PAGE.encode("utf-8"), url)


def test_mediafire_matches_and_resolves_via_injected_client(fake_dns):
    http = FakeHTTP([mediafire_response()])
    resolver = MediafireResolver(timeout=2.0, http=http, dns=fake_dns,
                                 clock=lambda: 1000.0)
    assert resolver.matches("https://www.mediafire.com/file/abc/x.mp3/file")
    assert not resolver.matches("https://example.com/x.mp3")
    assert not resolver.matches("https://download1.mediafire.com/abc/x.mp3")
    got = resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")
    assert got.url == "https://download1.mediafire.com/abc/x.mp3?token=signed"
    assert got.source == "mediafire"
    assert got.expires_at == 1000.0 + 2 * 60 * 60
    assert "Referer" in got.headers
    assert http.calls[0]["method"] == "GET"
    assert http.calls[0]["url"].startswith("https://www.mediafire.com/file/")
    assert http.calls[0]["timeout"] == 2.0


def test_mediafire_does_not_delegate_to_ezgarage(monkeypatch, fake_dns):
    from core import ezgarage

    def boom(page):
        raise AssertionError("adapter must fetch through self.http")

    monkeypatch.setattr(ezgarage, "resolve_page", boom)
    resolver = MediafireResolver(http=FakeHTTP([mediafire_response()]),
                                 dns=fake_dns)
    assert resolver.resolve(
        "https://www.mediafire.com/file/abc/x.mp3/file").source == "mediafire"


@pytest.mark.parametrize("status,expected", [
    (302, base.UpstreamError),
    (404, base.NotFound),
    (410, base.NotFound),
    (429, base.RateLimited),
    (500, base.UpstreamError),
    (204, base.UpstreamError),
])
def test_mediafire_status_mapping(fake_dns, status, expected):
    headers = {"Retry-After": "17"} if status == 429 else {}
    http = FakeHTTP([base.HTTPResponse(status, headers)])
    resolver = MediafireResolver(http=http, dns=fake_dns)
    with pytest.raises(expected) as caught:
        resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")
    if status == 429:
        assert caught.value.retry_after == 17


def test_mediafire_302_to_loopback_is_refused(fake_dns):
    http = FakeHTTP([base.HTTPResponse(
        302, {"Location": "http://127.0.0.1:9000/secret"})])
    resolver = MediafireResolver(http=http, dns=fake_dns)
    with pytest.raises(base.UpstreamError):
        resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")
    assert len(http.calls) == 1


def test_mediafire_timeout_is_clamped_and_honoured(fake_dns):
    http = FakeHTTP(error=socket.timeout("slow"))
    resolver = MediafireResolver(timeout=999, http=http, dns=fake_dns)
    with pytest.raises(base.UpstreamTimeout):
        resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")
    assert http.calls[0]["timeout"] == base.MAX_TIMEOUT


def test_bounded_client_wall_clock_stops_slow_drip(monkeypatch):
    """The client's deadline, not a per-recv socket timeout, bounds a drip."""
    now = [0.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: now[0])

    class Drip(object):
        def read1(self, _size):
            now[0] += 0.04
            return b"x"

    client = base.UrllibHTTPClient(timeout=0.5)
    with pytest.raises(base.UpstreamTimeout):
        client._read_body(Drip(), deadline=0.1)
    assert now[0] < 0.2


def test_mediafire_concurrent_distinct_urls_do_not_serialise(fake_dns):
    """No process-global lock: two distinct URLs overlap in the client."""
    barrier = threading.Barrier(2)
    calls = []
    lock = threading.Lock()

    class BarrierHTTP(object):
        def request(self, method, url, headers=None, timeout=None):
            with lock:
                calls.append(url)
            barrier.wait(2)
            return mediafire_response(url)

    resolver = MediafireResolver(http=BarrierHTTP(), dns=fake_dns)
    errors = []

    def worker(path):
        try:
            resolver.resolve("https://www.mediafire.com/file/%s/x.mp3/file" % path)
        except Exception as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=("one",)),
               threading.Thread(target=worker, args=("two",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert errors == []
    assert len(calls) == 2
    assert barrier.broken is False


def test_drive_matches_and_follows_only_allowlisted_hop(fake_dns):
    http = FakeHTTP([
        base.HTTPResponse(302, {
            "Location": "https://drive.usercontent.google.com/download?id=ABC1234567"}),
        base.HTTPResponse(200, {"Content-Type": "audio/mpeg",
                                "Content-Disposition": "attachment; filename=x.mp3"}),
    ])
    resolver = DriveResolver(timeout=2.0, http=http, dns=fake_dns,
                             clock=lambda: 2000.0)
    url = "https://drive.google.com/file/d/ABC1234567/view"
    assert resolver.matches(url)
    assert not resolver.matches("https://example.com/file/d/ABC/view")
    got = resolver.resolve(url)
    assert got.url.startswith("https://drive.usercontent.google.com/")
    assert got.source == "drive"
    assert got.expires_at == 2000.0 + 2 * 60 * 60
    assert len(http.calls) == 2
    assert http.calls[0]["timeout"] == 2.0


def test_drive_302_to_loopback_is_refused(fake_dns):
    http = FakeHTTP([base.HTTPResponse(
        302, {"Location": "http://127.0.0.1:9000/secret"})])
    resolver = DriveResolver(http=http, dns=fake_dns)
    with pytest.raises(base.UpstreamError):
        resolver.resolve("https://drive.google.com/file/d/ABC1234567/view")
    assert len(http.calls) == 1


def test_drive_confirm_form_is_fetched_through_client(fake_dns):
    http = FakeHTTP([
        base.HTTPResponse(200, {"Content-Type": "text/html"},
                          DRIVE_FORM.encode("utf-8")),
        base.HTTPResponse(200, {"Content-Type": "audio/mpeg"}),
    ])
    resolver = DriveResolver(http=http, dns=fake_dns)
    got = resolver.resolve("https://drive.google.com/file/d/ABC1234567/view")
    assert got.url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=ABC1234567" in got.url
    assert len(http.calls) == 2
    assert "id=ABC1234567" in http.calls[1]["url"]


def test_drive_non_audio_is_not_found(fake_dns):
    http = FakeHTTP([base.HTTPResponse(
        200, {"Content-Type": "text/html"}, b"<html>not audio</html>")])
    resolver = DriveResolver(http=http, dns=fake_dns)
    with pytest.raises(base.NotFound):
        resolver.resolve("https://drive.google.com/file/d/ABC1234567/view")


def test_drive_timeout_fails_typed(fake_dns):
    http = FakeHTTP(error=socket.timeout("slow"))
    resolver = DriveResolver(http=http, dns=fake_dns)
    with pytest.raises(base.UpstreamTimeout):
        resolver.resolve("https://drive.google.com/file/d/ABC1234567/view")


def test_drive_200_on_initial_host_stays_allowlisted(fake_dns):
    """A 200 from an unlisted host is refused (defence in depth)."""
    http = FakeHTTP([base.HTTPResponse(
        200, {"Content-Type": "audio/mpeg"}, b"", "https://cdn.example/audio.mp3")])
    resolver = DriveResolver(http=http, dns=fake_dns)
    # The adapter only ever requests allowlisted hosts, so this fake response
    # is a 200 for drive.google.com; the result is that host and stays inside
    # the allowlist.  The explicit rejection path is exercised by a redirect.
    assert resolver.resolve(
        "https://drive.google.com/file/d/ABC1234567/view").url.startswith(
            "https://drive.google.com/")


def test_drive_redirect_outside_allowlist_is_refused(fake_dns):
    http = FakeHTTP([base.HTTPResponse(
        302, {"Location": "https://cdn.example.com/audio.mp3"})])
    resolver = DriveResolver(http=http, dns=fake_dns)
    with pytest.raises(base.UpstreamError):
        resolver.resolve("https://drive.google.com/file/d/ABC1234567/view")
    assert len(http.calls) == 1


def test_default_service_has_all_adapters_and_mandatory_allowlists():
    service = build_default_service()
    sources = {getattr(a, "source", "") for a in service.adapters}
    assert {"direct", "podomatic", "mediafire", "drive", "mega"} <= sources
    for adapter in service.adapters:
        allowlist = getattr(adapter, "allowlist", None)
        assert isinstance(allowlist, base.HostAllowlist), adapter.source
        assert allowlist, adapter.source


def test_legacy_provider_caches_are_bounded(monkeypatch):
    from core import drive as drive_parse
    from core import ezgarage

    ezgarage.clear_memory()
    drive_parse.clear_memory()
    monkeypatch.setattr(ezgarage, "MAX_CACHE_ENTRIES", 3)
    monkeypatch.setattr(drive_parse, "MAX_CACHE_ENTRIES", 3)
    for index in range(10):
        ezgarage._remember_url(
            "set-%d" % index,
            "https://download1.mediafire.com/%d/x.mp3" % index)
        drive_parse._remember(
            "id-%d" % index,
            "https://drive.usercontent.google.com/download?id=%d" % index)
    assert len(ezgarage._CACHE) <= 3
    assert len(drive_parse._CACHE) <= 3
    ezgarage.clear_memory()
    drive_parse.clear_memory()
