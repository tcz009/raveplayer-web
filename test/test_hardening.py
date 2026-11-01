# -*- coding: utf-8 -*-
"""Hardening regressions from the adversarial pre-review (W2/W3/W5/W6/W7).

Everything runs offline: providers are monkeypatched and the on-disk cache /
favourites get a fresh directory per test.
"""

import email.message
import gzip
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------- W2 — non-finite durations


def test_human_duration_ignores_non_finite_values():
    import app as app_module

    assert app_module._human_duration(float("inf")) == ""
    assert app_module._human_duration(float("nan")) == ""
    assert app_module._human_duration("1e400") == ""
    assert app_module._human_duration("1872.46") == "31:12"


def _feed_xml(duration):
    return (
        "<?xml version='1.0'?><rss><channel><item><title>Show</title>"
        "<enclosure url='https://cdn.example/show.mp3' type='audio/mpeg'/>"
        "<itunes:duration xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.0'>%s"
        "</itunes:duration></item></channel></rss>" % duration
    )


def test_feed_duration_ignores_non_finite_metadata():
    from core import feeds

    assert feeds.parse(_feed_xml("inf"))[0]["duration"] is None
    assert feeds.parse(_feed_xml("1e400"))[0]["duration"] is None
    assert feeds.parse(_feed_xml("nan"))[0]["duration"] is None
    assert feeds.parse(_feed_xml("1:02:03.500"))[0]["duration"] == 3723


def test_feed_with_entity_declarations_is_refused():
    """Billion-laughs mirror: ElementTree expands internal entities."""
    from core import feeds

    bomb = (
        "<?xml version='1.0'?><!DOCTYPE r ["
        "<!ENTITY a 'lol'><!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'>]>"
        "<rss><channel><item><title>&b;</title>"
        "<enclosure url='https://cdn.example/x.mp3'/></item></channel></rss>"
    )
    assert feeds.parse(bomb) == []


def test_item_with_non_finite_length_is_not_a_500(web, monkeypatch):
    """W2: third-party metadata must not crash the item screen."""
    import app as app_module

    monkeypatch.setattr(app_module.archive, "audio_files", lambda identifier: [{
        "name": "a.mp3",
        "title": "Show A",
        "url": "https://archive.org/download/pack/a.mp3",
        "length": float("inf"),
    }])
    response = web.get("/api/item/pack-1")
    assert response.status_code == 200
    assert response.get_json()["items"][0]["sub"] == ""


# --------------------------------------------------------------------------- W3 — favourites race


def test_concurrent_favourite_adds_do_not_lose_entries(tmp_path, monkeypatch):
    """W3: load-modify-save is serialised by a thread lock + flock."""
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(tmp_path / "favs"))
    from core import favourites

    real_save = favourites.save

    def slow_save(entries):
        time.sleep(0.01)  # widen the race window if the lock is missing
        return real_save(entries)

    monkeypatch.setattr(favourites, "save", slow_save)
    workers = 8
    barrier = threading.Barrier(workers)
    errors = []

    def worker(index):
        barrier.wait()
        try:
            favourites.add({
                "kind": "stream",
                "target": "https://example.com/%d.mp3" % index,
                "title": "Track %d" % index,
            })
        except Exception as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    entries = favourites.load()
    assert len(entries) == workers, "concurrent adds lost entries: %d of %d" % (
        len(entries), workers)
    assert (tmp_path / "favs" / favourites.LOCKNAME).exists()


def test_concurrent_add_and_remove_keep_the_store_consistent(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(tmp_path / "favs"))
    from core import favourites

    favourites.add({"kind": "stream", "target": "https://example.com/a.mp3", "title": "A"})
    favourites.add({"kind": "stream", "target": "https://example.com/b.mp3", "title": "B"})
    key_a = favourites.key({"kind": "stream", "target": "https://example.com/a.mp3"})
    barrier = threading.Barrier(2)

    def remover():
        barrier.wait()
        favourites.remove(key_a)

    def adder():
        barrier.wait()
        favourites.add({"kind": "stream", "target": "https://example.com/c.mp3", "title": "C"})

    threads = [threading.Thread(target=remover), threading.Thread(target=adder)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    targets = sorted(entry["target"] for entry in favourites.load())
    assert targets == ["https://example.com/b.mp3", "https://example.com/c.mp3"]


# --------------------------------------------------------------------------- W5 — cache pruning


def _stale_cache_file(directory, name):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")
    old = time.time() - 30 * 24 * 3600
    os.utime(path, (old, old))
    return path


def test_cache_prune_runs_from_a_request_at_most_hourly(web, tmp_path, monkeypatch):
    """W5: pruning must happen while serving, not only at worker import."""
    import app as app_module
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    stale = _stale_cache_file(cache_dir, "a" * 64 + ".json")
    fresh = os.path.join(cache_dir, "b" * 64 + ".json")
    with open(fresh, "w", encoding="utf-8") as handle:
        handle.write("{}")

    app_module._PRUNE_STATE["at"] = 0.0
    assert web.get("/api/menu").status_code == 200
    assert not os.path.exists(stale), "a stale entry must be pruned from a request"
    assert os.path.exists(fresh), "fresh entries must be kept"

    # Inside the interval a second stale file must survive (bounded frequency).
    stale2 = _stale_cache_file(cache_dir, "c" * 64 + ".json")
    assert web.get("/api/menu").status_code == 200
    assert os.path.exists(stale2), "pruning must not run on every request"


def test_cache_prune_work_is_capped_per_run(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    total = app_module.PRUNE_MAX_REMOVALS + 25
    for index in range(total):
        _stale_cache_file(cache_dir, "%064x.json" % index)
    app_module._PRUNE_STATE["at"] = 0.0
    app_module._maybe_prune_cache()
    left = [name for name in os.listdir(cache_dir) if name.endswith(".json")]
    assert len(left) == total - app_module.PRUNE_MAX_REMOVALS


# --------------------------------------------------------------------------- W6 — docs vs runtime


def test_docs_match_the_container_interpreter_and_module_layout():
    dockerfile = _read("Dockerfile")
    version = re.search(r"^FROM python:([0-9.]+)-slim", dockerfile, re.MULTILINE).group(1)
    review = _read(os.path.join("docs", "CODE-REVIEW.md"))
    readme = _read("README.md")
    assert "python:%s-slim" % version in review
    assert "Python %s" % version in review
    assert "python:%s-slim" % version in readme
    assert "core/fetch.py" in review
    assert "core/http.py" not in review


# --------------------------------------------------------------------------- W7 — SPA link scheme


def test_spa_filters_external_links_to_http_schemes():
    """W7: provider data must not be able to set a javascript:/data: href."""
    js = _read(os.path.join("static", "app.js"))
    assert "innerHTML" not in js
    assert "safeExternalHref" in js
    assert "external.href = safeExternalHref(item.external" in js
    assert "external.href = item.external" not in js
    assert 'url.protocol === "http:" || url.protocol === "https:"' in js

# --------------------------------------------------------------------------- H1 — fetch redirects (SSRF)


def _redirect_request(url):
    """Run the redirect handler the same way urllib does, return the target."""
    from core import fetch as core_fetch

    request = urllib.request.Request("https://public.example/start")
    handler = core_fetch._SafeRedirectHandler()
    return handler.redirect_request(request, None, 302, "Found", {}, url)


def test_fetch_redirect_to_private_address_is_refused():
    from core import fetch as core_fetch

    for url in ("http://127.0.0.1:8097/healthz",
                "http://169.254.169.254/latest/meta-data/",
                "http://[::1]/x"):
        with pytest.raises(urllib.error.HTTPError):
            _redirect_request(url)
    # And the safe handler is actually installed on the module opener.
    assert any(isinstance(handler, core_fetch._SafeRedirectHandler)
               for handler in core_fetch._OPENER.handlers)


def test_fetch_redirect_to_bad_scheme_is_refused():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"):
        with pytest.raises(urllib.error.HTTPError):
            _redirect_request(url)


def test_fetch_redirect_chain_is_capped(monkeypatch):
    from core import fetch as core_fetch

    request = urllib.request.Request("https://public.example/start")
    request.raveweb_redirects = core_fetch.MAX_REDIRECTS
    handler = core_fetch._SafeRedirectHandler()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {},
                                 "https://public.example/next")


def test_fetch_allows_private_redirects_when_opted_in(monkeypatch):
    monkeypatch.setenv("RAVEWEB_ALLOW_PRIVATE", "1")
    from core import fetch as core_fetch

    target = core_fetch._validate_redirect("http://127.0.0.1:8097/healthz")
    assert target == "http://127.0.0.1:8097/healthz"


def test_fetch_refuses_a_real_redirect_to_a_private_address():
    """Wiring test: a real 302 to loopback must make fetch() fail closed."""
    import http.server
    import threading as _threading

    from core import fetch as core_fetch

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # pragma: no cover - should never be reached
            body = b"SECRET"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        target = 0

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/secret" % self.target)
            self.end_headers()

        def log_message(self, *args):
            pass

    try:
        target_server = http.server.HTTPServer(("127.0.0.1", 0), Target)
        redirect_server = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    except OSError:  # pragma: no cover - no loopback sockets in this sandbox
        pytest.skip("loopback sockets unavailable")
    Redirector.target = target_server.server_address[1]
    redirect_port = redirect_server.server_address[1]
    for server in (target_server, redirect_server):
        _threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert core_fetch.fetch("http://127.0.0.1:%d/redir" % redirect_port,
                                timeout=3) is None
    finally:
        for server in (target_server, redirect_server):
            server.shutdown()
            server.server_close()


# --------------------------------------------------------------------------- H2/H3 — fetch caps + log redaction


class _FakeResponse(object):
    def __init__(self, body, encoding=None):
        self._body = body
        self._offset = 0
        self.headers = email.message.Message()
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def read(self, size=None):
        chunk = self._body[self._offset:self._offset + (size or len(self._body))]
        self._offset += len(chunk)
        return chunk


def test_fetch_response_size_caps():
    from core import fetch as core_fetch

    limit = core_fetch.MAX_BODY_BYTES
    assert core_fetch._decode(_FakeResponse(b"hello")) == "hello"
    # Raw body over the cap.
    assert core_fetch._decode(_FakeResponse(b"x" * (limit + 1))) is None
    # gzip bomb: a small compressed body that expands past the cap.
    assert core_fetch._decode(_FakeResponse(gzip.compress(b"x" * (limit + 1)),
                                            encoding="gzip")) is None
    # deflate bomb: the decompressor stops at the cap with tail left over.
    assert core_fetch._decode(_FakeResponse(zlib.compress(b"x" * (limit + 1)),
                                            encoding="deflate")) is None


def test_fetch_failure_logs_redact_query_secrets(monkeypatch, caplog):
    from core import fetch as core_fetch

    class Boom(object):
        def open(self, request, timeout=None):
            raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(core_fetch, "_OPENER", Boom())
    caplog.set_level("DEBUG", logger="raveweb")
    assert core_fetch.fetch(
        "https://api.soundcloud.com/tracks?client_id=SECRET&q=kool") is None
    assert "SECRET" not in caplog.text
    assert "api.soundcloud.com/tracks" in caplog.text


# --------------------------------------------------------------------------- H4 — radio failover budget


def test_radio_mirror_failover_is_bounded(monkeypatch):
    from core import radio

    clock = {"now": 0.0}

    class FakeTime(object):
        @staticmethod
        def monotonic():
            return clock["now"]

    monkeypatch.setattr(radio, "time", FakeTime())
    calls = []

    def dead_fetch(url, timeout=20, headers=None, ttl=0):
        calls.append(timeout)
        clock["now"] += 10.0  # each dead mirror burns more than half the budget
        return None

    monkeypatch.setattr(radio.http, "fetch_json", dead_fetch)
    assert radio.stations("jungle") == []
    # The deadline (15 s) stops the third mirror: two attempts of <= 8 s each
    # instead of three 20 s timeouts.
    assert len(calls) <= 2
    assert all(timeout <= radio.MIRROR_TIMEOUT for timeout in calls)


# --------------------------------------------------------------------------- H5 — parallel fan-out


def _parallel_provider(name, barrier, reached):
    def run(*args, **kwargs):
        barrier.wait(timeout=3)
        reached.append(name)
        return [{
            "title": "%s set" % name,
            "user": "dj",
            "thumb": "https://cdn.example/%s.jpg" % name,
            "embed": "https://www.mixcloud.com/widget/iframe/?feed=x",
            "url": "https://www.mixcloud.com/dj/%s/" % name,
        }]

    return run


def test_feed_counts_run_in_parallel(monkeypatch):
    from core import feeds

    barrier = threading.Barrier(2)
    reached = []

    def fake_count(feed_id):
        barrier.wait(timeout=3)
        reached.append(feed_id)
        return 3

    monkeypatch.setattr(feeds, "count", fake_count)
    assert feeds.counts(["one", "two"]) == {"one": 3, "two": 3}
    assert not barrier.broken
    assert sorted(reached) == ["one", "two"]


def test_search_runs_soundcloud_and_mixcloud_concurrently(web, archive_search, monkeypatch):
    import app as app_module

    barrier = threading.Barrier(2)
    reached = []
    monkeypatch.setattr(app_module.soundcloud, "configured", lambda: True)
    monkeypatch.setattr(app_module.soundcloud, "search",
                        _parallel_provider("soundcloud", barrier, reached))
    monkeypatch.setattr(app_module.mixcloud, "search",
                        _parallel_provider("mixcloud", barrier, reached))
    payload = web.get("/api/search?q=kool").get_json()
    assert not barrier.broken, "providers ran one after another"
    assert sorted(reached) == ["mixcloud", "soundcloud"]
    assert len(payload["items"]) == 3  # pack + soundcloud + mixcloud rows
