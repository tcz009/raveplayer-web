# -*- coding: utf-8 -*-
"""Offline tests for the resolver service and its simple adapters.

No test touches DNS or the network: every adapter/service gets a fake DNS
resolver, and the optional HEAD probes get a fake HTTP client.
"""

import socket
import threading
import time

import pytest
from flask import Flask

from core.resolver import base
from core.resolver import cache as resolver_cache
from core.resolver import direct, podomatic
from core.resolver import service as resolver_service
from core.resolver.service import ResolverService, TokenBucket, resolver_bp


# --------------------------------------------------------------------------- fakes


class FakeClock(object):
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


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
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, url, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.response


class EchoAdapter(object):
    """Matches everything and returns the input URL unless configured to fail."""

    source = "echo"
    # Every real adapter must publish one; the test adapter covers the hosts
    # used by this module (the service now fails closed without it).
    allowlist = base.HostAllowlist(
        ("example", "example.com", "mediafire.com", "mega.nz"))

    def __init__(self, error=None, result=None, calls=None):
        self.error = error
        self.result = result
        self.calls = calls if calls is not None else []

    def matches(self, url):
        return True

    def resolve(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return base.Resolved(url=url, headers={}, expires_at=time.time() + 3600,
                             source=self.source)


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def fake_dns():
    return FakeDNS()


@pytest.fixture()
def flask_app():
    app = Flask(__name__)
    app.secret_key = "resolver-test"
    app.config.update(TESTING=True)
    app.register_blueprint(resolver_bp)
    return app


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture()
def authed(flask_app):
    flask_app.config["RESOLVER_AUTH_CHECK"] = lambda: True
    return flask_app


def make_service(fake_dns, clock, adapters, rate=0, burst=10):
    return ResolverService(
        adapters=adapters,
        cache=resolver_cache.ResolverCache(clock=clock),
        limiter=TokenBucket(rate_per_minute=rate, burst=burst),
        dns=fake_dns,
        clock=clock,
    )


def make_direct_service(fake_dns, clock, hosts=("cdn.example.com",), **kwargs):
    adapter = direct.DirectResolver(
        allowed_hosts=hosts, dns=fake_dns, clock=clock, **kwargs)
    return make_service(fake_dns, clock, [adapter])


def attach(flask_app, service):
    flask_app.config["RESOLVER_SERVICE"] = service
    return service


# --------------------------------------------------------------------------- base


def test_normalize_url_canonicalises_and_strips_secrets():
    assert base.normalize_url(
        "HTTPS://CDN.Example.com:443/a.MP3?x=1#frag"
    ) == "https://cdn.example.com/a.MP3?x=1"
    assert base.normalize_url("http://example.com") == "http://example.com/"
    assert base.url_host("https://CDN.Example.com/a.mp3") == "cdn.example.com"
    assert base.is_audio_url("https://example.com/a.mp3?x=.zip")
    assert base.is_audio_url("https://example.com/a.M4A")
    assert not base.is_audio_url("https://example.com/a.mp4")


@pytest.mark.parametrize("bad", ["", "   ", "ftp://example.com/a.mp3",
                                 "https:///a.mp3", "https://example.com/" + "a" * 3000,
                                 "https://example.com/a b.mp3",
                                 "https://example.com/a\\b.mp3"])
def test_normalize_url_rejects_bad_input(bad):
    with pytest.raises(base.ResolverError):
        base.normalize_url(bad)


def test_credentials_are_blocked():
    with pytest.raises(base.BlockedURL):
        base.normalize_url("https://user:pass@example.com/a.mp3")


def test_host_allowlist_is_dot_boundary_safe():
    allow = base.HostAllowlist(["example.com", "archive.org"])
    assert allow.allows("example.com")
    assert allow.allows("cdn.example.com")
    assert not allow.allows("notexample.com")
    assert allow.allows("archive.org")
    assert not allow.allows("evil.archive.org.evil.test")
    with pytest.raises(ValueError):
        base.HostAllowlist(["*.example.com"])
    with pytest.raises(ValueError):
        base.HostAllowlist(["bad/path"])


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.1.2.3",
                                  "169.254.169.254", "::1", "fd00::1",
                                  "docker-service", "printer.local"])
def test_private_and_loopback_hosts_are_rejected(host):
    assert base.is_private_hostname(host)
    with pytest.raises(base.BlockedURL):
        base.check_public_host(host, 80)


def test_dns_is_checked_for_public_hosts(fake_dns):
    assert base.assert_public_url("https://cdn.example.com/a.mp3",
                                  dns=fake_dns) == "https://cdn.example.com/a.mp3"
    assert fake_dns.calls == [("cdn.example.com", 443)]


def test_dns_private_answer_is_rejected(fake_dns):
    fake_dns.addresses = ["127.0.0.1"]
    with pytest.raises(base.BlockedURL):
        base.assert_public_url("https://cdn.example.com/a.mp3", dns=fake_dns)


def test_dns_failure_is_blocked(fake_dns):
    fake_dns.error = socket.gaierror("nope")
    with pytest.raises(base.BlockedURL):
        base.assert_public_url("https://cdn.example.com/a.mp3", dns=fake_dns)


def test_header_sanitising_rejects_credentials_and_crlf():
    assert base.sanitize_headers({"Referer": "https://rave.example/"}) == {
        "Referer": "https://rave.example/"}
    with pytest.raises(ValueError):
        base.sanitize_headers({"Cookie": "sid=secret"})
    with pytest.raises(ValueError):
        base.sanitize_headers({"X-Test": "a\r\nInjected: yes"})
    with pytest.raises(ValueError):
        base.sanitize_headers({"Host": "evil.example"})


def test_timeout_is_bounded():
    assert base.clamp_timeout(999) == base.MAX_TIMEOUT
    assert base.clamp_timeout(0) == base.MIN_TIMEOUT
    assert base.clamp_timeout("nonsense") == base.DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- direct


def test_direct_matches_only_allowlisted_audio(fake_dns, clock):
    resolver = direct.DirectResolver(
        allowed_hosts=["cdn.example.com"], dns=fake_dns, clock=clock)
    assert resolver.matches("https://cdn.example.com/a.mp3")
    assert resolver.matches("https://CDN.EXAMPLE.COM/a.M4A?x=1")
    assert not resolver.matches("https://cdn.example.com/a.mp4")
    assert not resolver.matches("https://other.example.com/a.mp3")
    assert not resolver.matches("ftp://cdn.example.com/a.mp3")
    # Podomatic owns pdcn links so they are canonicalised, not passed through.
    assert not direct.DirectResolver(allowed_hosts=[]).matches(PDCN_URL)


def test_direct_resolves_with_headers_and_expiry(fake_dns, clock):
    resolver = direct.DirectResolver(
        allowed_hosts=["cdn.example.com"],
        headers={"Referer": "https://rave.example/"},
        ttl=120,
        dns=fake_dns,
        clock=clock,
    )
    resolved = resolver.resolve(
        "HTTPS://CDN.Example.com:443/a.MP3?x=1#frag")
    assert resolved.url == "https://cdn.example.com/a.MP3?x=1"
    assert resolved.headers == {"Referer": "https://rave.example/"}
    assert resolved.expires_at == 1120.0
    assert resolved.source == "direct"


def test_direct_rejects_host_outside_allowlist(fake_dns, clock):
    resolver = direct.DirectResolver(
        allowed_hosts=["cdn.example.com"], dns=fake_dns, clock=clock)
    with pytest.raises(base.BlockedURL) as exc:
        resolver.resolve("https://evil.example/a.mp3")
    assert exc.value.status == 400
    assert exc.value.code == "blocked"


def test_direct_rejects_allowlisted_private_host(fake_dns, clock):
    resolver = direct.DirectResolver(
        allowed_hosts=["127.0.0.1"], dns=fake_dns, clock=clock)
    with pytest.raises(base.BlockedURL):
        resolver.resolve("http://127.0.0.1/a.mp3")


def test_direct_rejects_non_audio(fake_dns, clock):
    resolver = direct.DirectResolver(
        allowed_hosts=["cdn.example.com"], dns=fake_dns, clock=clock)
    with pytest.raises(base.UnsupportedMedia):
        resolver.resolve("https://cdn.example.com/show")


@pytest.mark.parametrize("status,error,expected", [
    (200, None, None),
    (404, None, base.NotFound),
    (410, None, base.NotFound),
    (503, None, base.UpstreamError),
    (403, None, base.UpstreamError),
    (302, None, base.UpstreamError),
    (200, socket.timeout("slow"), base.UpstreamTimeout),
])
def test_direct_optional_probe_maps_upstream_failures(
        fake_dns, clock, status, error, expected):
    response = base.HTTPResponse(status, {"Content-Type": "audio/mpeg"})
    http = FakeHTTP(response=response, error=error)
    resolver = direct.DirectResolver(
        allowed_hosts=["cdn.example.com"], dns=fake_dns, clock=clock,
        http=http, verify=True)
    if expected is None:
        assert resolver.resolve("https://cdn.example.com/a.mp3").url.endswith(".mp3")
        assert http.calls[0]["method"] == "HEAD"
    else:
        with pytest.raises(expected):
            resolver.resolve("https://cdn.example.com/a.mp3")


def test_direct_probe_rejects_html(fake_dns, clock):
    response = base.HTTPResponse(200, {"Content-Type": "text/html"})
    resolver = direct.DirectResolver(
        allowed_hosts=["cdn.example.com"], dns=fake_dns, clock=clock,
        http=FakeHTTP(response=response), verify=True)
    with pytest.raises(base.UnsupportedMedia):
        resolver.resolve("https://cdn.example.com/a.mp3")


# --------------------------------------------------------------------------- podomatic


PDCN_URL = ("https://pdcn.co/e/dejavufmpodcast.podomatic.com/enclosure/"
            "2026-11-07T09_15_50-08_00.mp3?_=1794071789.17925363")
PDCN_CANONICAL = ("https://dejavufmpodcast.podomatic.com/enclosure/"
                  "2026-11-07T09_15_50-08_00.mp3?_=1794071789.17925363")


def test_podomatic_matches_only_its_hosts(fake_dns, clock):
    resolver = podomatic.PodomaticResolver(dns=fake_dns, clock=clock)
    assert resolver.matches(PDCN_URL)
    assert resolver.matches("https://ratpack.podomatic.com/enclosure/x.mp3")
    assert not resolver.matches("https://example.com/x.mp3")


def test_podomatic_pdcn_link_is_canonicalised(fake_dns, clock):
    resolver = podomatic.PodomaticResolver(dns=fake_dns, clock=clock)
    resolved = resolver.resolve(PDCN_URL)
    assert resolved.url == PDCN_CANONICAL
    assert resolved.source == "podomatic"
    assert "Podomatic" not in resolved.headers.get("Referer", "")
    assert resolved.headers["Referer"] == "https://www.podomatic.com/"
    assert resolved.headers["User-Agent"].startswith("RavePlayer/")
    assert resolved.expires_at == 4600.0


def test_podomatic_percent_encoded_pdcn_link(fake_dns, clock):
    url = ("https://pdcn.co/e/https%3A%2F%2Fdejavufmpodcast.podomatic.com"
           "%2Fenclosure%2Fx.mp3")
    resolved = podomatic.PodomaticResolver(dns=fake_dns, clock=clock).resolve(url)
    assert resolved.url == "https://dejavufmpodcast.podomatic.com/enclosure/x.mp3"


def test_podomatic_plain_pdcn_link_keeps_encoded_filename(fake_dns, clock):
    url = "https://pdcn.co/e/show.podomatic.com/enclosure/a%20b.mp3"
    resolved = podomatic.PodomaticResolver(dns=fake_dns, clock=clock).resolve(url)
    assert resolved.url == "https://show.podomatic.com/enclosure/a%20b.mp3"


def test_podomatic_direct_enclosure_uses_https(fake_dns, clock):
    resolved = podomatic.PodomaticResolver(dns=fake_dns, clock=clock).resolve(
        "http://ratpack.podomatic.com/enclosure/x.mp3?_=1")
    assert resolved.url == "https://ratpack.podomatic.com/enclosure/x.mp3?_=1"


@pytest.mark.parametrize("url", [
    "https://www.podomatic.com/podcasts/dejavufmpodcast/episodes/2026-11-07T09_15_50-08_00",
    "https://www.podomatic.com/podcasts/dejavufmpodcast",
    "https://ratpack.podomatic.com/rss2.xml",
    "https://ratpack.podomatic.com/enclosure/show.m3u",
    "https://pdcn.co/e/evil.example/enclosure/x.mp3",
    "https://pdcn.co/e/ratpack.podomatic.com/playlist.m3u",
    "https://pdcn.co/not/e/x.mp3",
])
def test_podomatic_rejects_pages_and_playlists(fake_dns, clock, url):
    with pytest.raises(base.ResolverError) as exc:
        podomatic.PodomaticResolver(dns=fake_dns, clock=clock).resolve(url)
    assert exc.value.status == 400


def test_podomatic_rejects_private_destination(fake_dns, clock):
    fake_dns.addresses = ["192.168.1.10"]
    with pytest.raises(base.BlockedURL):
        podomatic.PodomaticResolver(dns=fake_dns, clock=clock).resolve(PDCN_URL)


def test_podomatic_optional_probe(fake_dns, clock):
    response = base.HTTPResponse(200, {"Content-Type": "audio/mpeg"})
    http = FakeHTTP(response=response)
    resolved = podomatic.PodomaticResolver(
        dns=fake_dns, clock=clock, http=http, verify=True).resolve(PDCN_URL)
    assert resolved.url == PDCN_CANONICAL
    assert http.calls[0]["url"] == PDCN_CANONICAL


# --------------------------------------------------------------------------- cache


def test_cache_positive_ttl(clock):
    cache = resolver_cache.ResolverCache(positive_ttl=60, clock=clock)
    resolved = base.Resolved("https://cdn.example.com/a.mp3", {}, 5000, "direct")
    assert cache.store("k", resolved)
    assert cache.get("k") is resolved
    clock.advance(59.9)
    assert cache.lookup("k") is not None
    clock.advance(0.1)
    assert cache.lookup("k") is None


def test_cache_respects_shorter_url_expiry(clock):
    cache = resolver_cache.ResolverCache(positive_ttl=600, clock=clock)
    resolved = base.Resolved("https://cdn.example.com/a.mp3", {}, 1010, "direct")
    cache.store("k", resolved)
    clock.advance(11)
    assert cache.lookup("k") is None


def test_cache_negative_ttl_and_only_definitive_errors(clock):
    cache = resolver_cache.ResolverCache(negative_ttl=15, clock=clock)
    assert cache.store_error("nf", base.NotFound("gone"))
    with pytest.raises(base.NotFound):
        cache.get("nf")
    clock.advance(15)
    assert cache.lookup("nf") is None
    assert not cache.store_error("timeout", base.UpstreamTimeout("slow"))
    assert not cache.store_error("rate", base.RateLimited("slow", retry_after=1))
    assert not cache.store_error("blocked", base.BlockedURL("no"))
    assert not cache.store_error("unknown", base.UnknownURL("no"))


def test_cache_single_flight_same_url(clock):
    cache = resolver_cache.ResolverCache(clock=clock)
    calls = []
    started = threading.Event()
    release = threading.Event()
    results = []

    def factory():
        calls.append(threading.current_thread().name)
        started.set()
        assert release.wait(2)
        return base.Resolved("https://cdn.example.com/a.mp3", {},
                             clock.now + 60, "direct")

    def worker():
        results.append(cache.run("key", "cdn.example.com", factory))

    first = threading.Thread(target=worker)
    first.start()
    assert started.wait(2)
    second = threading.Thread(target=worker)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(2)
    second.join(2)
    assert len(calls) == 1
    assert len(results) == 2
    assert results[0] == results[1]


def test_cache_serialises_different_urls_per_host(clock):
    cache = resolver_cache.ResolverCache(clock=clock)
    calls = []
    first_started = threading.Event()
    release = threading.Event()
    second_attempted = threading.Event()

    def factory_a():
        calls.append("a")
        first_started.set()
        assert release.wait(2)
        return base.Resolved("https://cdn.example.com/a.mp3", {},
                             clock.now + 60, "direct")

    def factory_b():
        calls.append("b")
        return base.Resolved("https://cdn.example.com/b.mp3", {},
                             clock.now + 60, "direct")

    def worker(factory):
        if factory is factory_b:
            second_attempted.set()
        cache.run("key-" + ("a" if factory is factory_a else "b"),
                  "cdn.example.com", factory)

    first = threading.Thread(target=worker, args=(factory_a,))
    first.start()
    assert first_started.wait(2)
    second = threading.Thread(target=worker, args=(factory_b,))
    second.start()
    assert second_attempted.wait(2)
    time.sleep(0.05)
    assert calls == ["a"]  # host slot held by the first request
    release.set()
    first.join(2)
    second.join(2)
    assert calls == ["a", "b"]


# --------------------------------------------------------------------------- service


def test_service_get_contract(client, authed, flask_app, fake_dns, clock):
    service = attach(flask_app, make_direct_service(
        fake_dns, clock, headers={"Referer": "https://rave.example/"}))
    response = client.get("/api/resolve?u=https://cdn.example.com/a.mp3")
    assert response.status_code == 200
    assert "Location" not in response.headers
    payload = response.get_json()
    assert set(payload) == {"ok", "url", "headers", "expires", "source", "host"}
    assert payload == {
        "ok": True,
        "url": "https://cdn.example.com/a.mp3",
        "headers": {"Referer": "https://rave.example/"},
        "expires": 4600,
        "source": "direct",
        "host": "cdn.example.com",
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert service.adapters


def test_service_post_contract(client, authed, flask_app, fake_dns, clock):
    attach(flask_app, make_direct_service(fake_dns, clock))
    response = client.post("/api/resolve",
                           json={"url": "https://cdn.example.com/a.m4a"})
    assert response.status_code == 200
    assert response.get_json()["url"] == "https://cdn.example.com/a.m4a"
    assert response.get_json()["source"] == "direct"


def test_service_requires_auth(client, fake_dns):
    response = client.get("/api/resolve?u=https://cdn.example.com/a.mp3")
    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"] == "authentication required"
    assert payload["code"] == "unauthorized"
    assert payload["ok"] is False


def test_service_auth_check_fails_closed(client, flask_app):
    flask_app.config["RESOLVER_AUTH_CHECK"] = lambda: False
    assert client.get("/api/resolve?u=https://cdn.example.com/a.mp3").status_code == 401


def test_service_missing_and_bad_requests(client, authed, flask_app, fake_dns, clock):
    attach(flask_app, make_direct_service(fake_dns, clock))
    assert client.get("/api/resolve").status_code == 400
    assert client.get("/api/resolve").get_json()["code"] == "invalid"
    assert client.post("/api/resolve", data="not json",
                       content_type="application/json").status_code == 400
    assert client.post("/api/resolve", json={"url": 123}).status_code == 400
    huge = "https://cdn.example.com/" + ("a" * 9000) + ".mp3"
    assert client.post("/api/resolve", json={"url": huge}).status_code == 400
    assert client.get("/api/resolve?u=" + "x" * 3000).status_code == 400


def test_service_rejects_private_and_loopback(client, authed, flask_app,
                                              fake_dns, clock):
    attach(flask_app, make_direct_service(fake_dns, clock))
    for url in ("http://127.0.0.1/a.mp3", "http://localhost/a.mp3",
                "http://[::1]/a.mp3", "http://169.254.169.254/latest/meta-data"):
        response = client.get("/api/resolve", query_string={"u": url})
        assert response.status_code == 400, url
        assert response.get_json()["code"] == "blocked", url


def test_service_rejects_host_resolving_private(client, authed, flask_app,
                                                fake_dns, clock):
    attach(flask_app, make_direct_service(fake_dns, clock))
    fake_dns.addresses = ["10.0.0.5"]
    response = client.get(
        "/api/resolve?u=https://cdn.example.com/a.mp3")
    assert response.status_code == 400
    assert response.get_json()["code"] == "blocked"


def test_service_maps_dns_timeout_to_504(client, authed, flask_app,
                                          fake_dns, clock):
    attach(flask_app, make_direct_service(fake_dns, clock))
    fake_dns.error = socket.timeout("slow")
    response = client.get("/api/resolve?u=https://cdn.example.com/a.mp3")
    assert response.status_code == 504
    assert response.get_json()["code"] == "timeout"


def test_service_rejects_arbitrary_host(client, authed, flask_app,
                                        fake_dns, clock):
    attach(flask_app, make_direct_service(fake_dns, clock))
    response = client.get("/api/resolve?u=https://evil.example/a.mp3")
    assert response.status_code == 400
    assert response.get_json()["code"] == "unknown"


@pytest.mark.parametrize("error,status,code", [
    (base.NotFound("gone"), 404, "not_found"),
    (base.UnsupportedMedia("playlist"), 400, "unsupported"),
    (base.BlockedURL("no"), 400, "blocked"),
    (base.UnknownURL("no"), 400, "unknown"),
    (base.UpstreamError("bad"), 502, "upstream"),
    (base.UpstreamTimeout("slow"), 504, "timeout"),
    (base.RateLimited("slow", retry_after=7), 429, "rate_limited"),
])
def test_service_maps_adapter_errors(client, authed, flask_app, fake_dns,
                                     clock, error, status, code):
    adapter = EchoAdapter(error=error)
    attach(flask_app, make_service(fake_dns, clock, [adapter]))
    response = client.get("/api/resolve?u=https://fake.example/x")
    assert response.status_code == status
    payload = response.get_json()
    assert payload["code"] == code
    assert payload["ok"] is False
    if status == 429:
        assert response.headers["Retry-After"] == "7"
        assert payload["retry_after"] == 7


def test_service_maps_unexpected_adapter_failure_to_502(client, authed,
                                                         flask_app, fake_dns,
                                                         clock):
    attach(flask_app, make_service(
        fake_dns, clock, [EchoAdapter(error=RuntimeError("boom"))]))
    response = client.get("/api/resolve?u=https://fake.example/x")
    assert response.status_code == 502
    assert response.get_json()["code"] == "upstream"


def test_service_rejects_adapter_that_returns_a_private_url(
        client, authed, flask_app, fake_dns, clock):
    result = base.Resolved("http://127.0.0.1/steal", {}, 5000, "echo")
    attach(flask_app, make_service(
        fake_dns, clock, [EchoAdapter(result=result)]))
    response = client.get("/api/resolve?u=https://fake.example/x")
    assert response.status_code == 502
    assert response.get_json()["code"] == "upstream"


def test_service_enforces_adapter_result_allowlist(client, authed, flask_app,
                                                    fake_dns, clock):
    class AllowlistedAdapter(EchoAdapter):
        allowlist = base.HostAllowlist(["cdn.example.com"])

    result = base.Resolved("https://evil.example/x.mp3", {}, 5000, "echo")
    attach(flask_app, make_service(
        fake_dns, clock, [AllowlistedAdapter(result=result)]))
    response = client.get("/api/resolve?u=https://fake.example/x")
    assert response.status_code == 502
    assert response.get_json()["code"] == "upstream"


def test_service_rejects_unsafe_adapter_headers(client, authed, flask_app,
                                                fake_dns, clock):
    result = base.Resolved("https://cdn.example.com/a.mp3",
                           {"Set-Cookie": "sid=secret"}, 5000, "echo")
    attach(flask_app, make_service(
        fake_dns, clock, [EchoAdapter(result=result)]))
    response = client.get("/api/resolve?u=https://fake.example/x")
    assert response.status_code == 502


def test_service_per_host_rate_limit(client, authed, flask_app, fake_dns, clock):
    limiter = TokenBucket(rate_per_minute=1, burst=1, clock=FakeClock())
    service = ResolverService(
        adapters=[EchoAdapter()],
        cache=resolver_cache.ResolverCache(clock=clock),
        limiter=limiter,
        dns=fake_dns,
        clock=clock,
    )
    attach(flask_app, service)
    first = client.get("/api/resolve?u=https://fake.example/one.mp3")
    assert first.status_code == 200
    second = client.get("/api/resolve?u=https://fake.example/two.mp3")
    assert second.status_code == 429
    assert second.get_json()["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) >= 1
    # A different host has its own bucket.
    third = client.get("/api/resolve?u=https://other.example/one.mp3")
    assert third.status_code == 200


def test_service_caches_successful_resolution(client, authed, flask_app,
                                               fake_dns, clock):
    adapter = EchoAdapter()
    attach(flask_app, make_service(fake_dns, clock, [adapter]))
    url = "https://fake.example/cache.mp3"
    first = client.get("/api/resolve", query_string={"u": url})
    second = client.get("/api/resolve", query_string={"u": url})
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json()
    assert len(adapter.calls) == 1


def test_service_negative_cache(client, authed, flask_app, fake_dns, clock):
    adapter = EchoAdapter(error=base.NotFound("gone"))
    attach(flask_app, make_service(fake_dns, clock, [adapter]))
    url = "https://fake.example/missing.mp3"
    assert client.get("/api/resolve", query_string={"u": url}).status_code == 404
    assert client.get("/api/resolve", query_string={"u": url}).status_code == 404
    assert len(adapter.calls) == 1


def test_service_timeouts_are_not_cached(client, authed, flask_app,
                                         fake_dns, clock):
    adapter = EchoAdapter(error=base.UpstreamTimeout("slow"))
    attach(flask_app, make_service(fake_dns, clock, [adapter]))
    url = "https://fake.example/slow.mp3"
    assert client.get("/api/resolve", query_string={"u": url}).status_code == 504
    assert client.get("/api/resolve", query_string={"u": url}).status_code == 504
    assert len(adapter.calls) == 2


def test_service_podomatic_end_to_end(client, authed, flask_app,
                                       fake_dns, clock):
    adapter = podomatic.PodomaticResolver(dns=fake_dns, clock=clock)
    attach(flask_app, make_service(fake_dns, clock, [adapter]))
    response = client.get("/api/resolve", query_string={"u": PDCN_URL})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["url"] == PDCN_CANONICAL
    assert payload["host"] == "dejavufmpodcast.podomatic.com"
    assert payload["source"] == "podomatic"


def test_build_default_service_is_offline_until_used(fake_dns, clock, monkeypatch):
    # Keep this test independent of sibling provider adapter files.
    monkeypatch.setenv("RESOLVER_ADAPTERS", "no-optional-adapter")
    service = resolver_service.build_default_service(dns=fake_dns, clock=clock)
    sources = [adapter.source for adapter in service.adapters]
    assert sources[:1] == ["podomatic"]
    assert "direct" in sources
    assert fake_dns.calls == []


# --------------------------------------------------------------------------- hardening


def test_normalize_url_restricts_ports():
    assert base.normalize_url("https://cdn.example.com:443/a.mp3") == \
        "https://cdn.example.com/a.mp3"
    assert base.normalize_url("http://cdn.example.com:80/a.mp3") == \
        "http://cdn.example.com/a.mp3"
    for url in ("https://cdn.example.com:8443/a.mp3",
                "https://cdn.example.com:22/a.mp3",
                "http://cdn.example.com:8080/a.mp3"):
        with pytest.raises(base.InvalidRequest):
            base.normalize_url(url)


def test_validate_resolved_rejects_non_web_port(clock):
    resolved = base.Resolved(
        "https://cdn.example.com:8443/a.mp3", {}, clock.now + 60, "direct")
    with pytest.raises(base.UpstreamError):
        base.validate_resolved(resolved, now=clock.now)


def test_check_public_host_restricts_ports(fake_dns):
    with pytest.raises(base.BlockedURL):
        base.check_public_host("cdn.example.com", 8443, dns=fake_dns)
    assert fake_dns.calls == []
    assert base.check_public_host("cdn.example.com", 443, dns=fake_dns)


def test_registrable_domain_buckets_provider_subdomains():
    assert base.registrable_domain("n12.mediafire.com") == "mediafire.com"
    assert base.registrable_domain("gfs208n127.userstorage.mega.co.nz") == \
        "mega.co.nz"
    assert base.registrable_domain("www.google.com") == "google.com"
    assert base.registrable_domain("93.184.216.34") == "93.184.216.34"


def test_auth_fails_closed_without_app_checker(client, monkeypatch):
    """auth+sid alone must never authenticate once the app check is gone."""
    def unavailable(name, package=None):
        raise ImportError("standalone mount")

    monkeypatch.setattr(resolver_service.importlib, "import_module", unavailable)
    with client.session_transaction() as sess:
        sess["auth"] = True
        sess["sid"] = "looks-like-a-session"
        sess["identity"] = "owner"
        sess["role"] = "admin"
    response = client.get("/api/resolve?u=https://cdn.example.com/a.mp3")
    assert response.status_code == 401
    assert response.get_json()["code"] == "unauthorized"


def test_service_identity_quota_hook(client, authed, flask_app, fake_dns, clock):
    service = ResolverService(
        adapters=[EchoAdapter()],
        cache=resolver_cache.ResolverCache(clock=clock),
        limiter=TokenBucket(rate_per_minute=0),
        dns=fake_dns,
        clock=clock,
        identity_quota=lambda identity: 5 if identity == "friends" else 0,
    )
    attach(flask_app, service)
    with client.session_transaction() as sess:
        sess["identity"] = "friends"
    response = client.get("/api/resolve?u=https://fake.example/a.mp3")
    assert response.status_code == 429
    assert response.get_json()["retry_after"] == 5


def test_service_cache_key_does_not_contain_the_url(fake_dns, clock):
    service = make_service(fake_dns, clock, [EchoAdapter()])
    service.resolve("https://fake.example/x.mp3#FRAGMENT-SECRET")
    keys = list(service.cache._entries)
    assert keys
    for key in keys:
        assert len(key) == 64
        assert "FRAGMENT-SECRET" not in key
        assert "fake.example" not in key


def test_token_bucket_buckets_by_registrable_domain(
        client, authed, flask_app, fake_dns, clock):
    service = ResolverService(
        adapters=[EchoAdapter()],
        cache=resolver_cache.ResolverCache(clock=clock),
        limiter=TokenBucket(rate_per_minute=1, burst=1, clock=FakeClock()),
        dns=fake_dns,
        clock=clock,
    )
    attach(flask_app, service)
    first = client.get("/api/resolve?u=https://n1.mediafire.com/a.mp3")
    assert first.status_code == 200
    second = client.get("/api/resolve?u=https://n2.mediafire.com/b.mp3")
    assert second.status_code == 429


def test_token_bucket_state_is_bounded():
    bucket = TokenBucket(rate_per_minute=600, burst=1,
                         clock=FakeClock(), max_keys=4)
    for index in range(50):
        bucket.take("host-%d.example" % index)
    assert len(bucket) == 4


def test_cache_host_locks_are_bounded(clock):
    cache = resolver_cache.ResolverCache(clock=clock, max_host_locks=2)
    for index in range(10):
        cache.run(
            "key-%d" % index,
            "h%d.example" % index,
            lambda: base.Resolved("https://cdn.example.com/a.mp3", {},
                                  clock.now + 60, "direct"),
        )
    assert len(cache._host_locks) <= 2


def test_adapter_without_allowlist_fails_closed(fake_dns, clock):
    class NoAllowlistAdapter(EchoAdapter):
        allowlist = None

    service = ResolverService(
        adapters=[NoAllowlistAdapter()],
        cache=resolver_cache.ResolverCache(clock=clock),
        limiter=TokenBucket(rate_per_minute=0),
        dns=fake_dns,
        clock=clock,
    )
    with pytest.raises(base.UpstreamError):
        service.resolve("https://fake.example/x")
