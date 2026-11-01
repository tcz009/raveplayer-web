# -*- coding: utf-8 -*-
"""Hardening regressions from the adversarial pre-review (W2/W3/W5/W6/W7).

Everything runs offline: providers are monkeypatched and the on-disk cache /
favourites get a fresh directory per test.
"""

import email.message
import gzip
import json
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


def _wait_for(condition, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _wait_prune_idle():
    import app as app_module
    return _wait_for(lambda: not app_module._PRUNE_LOCK.locked())


def test_cache_prune_runs_from_a_request_at_most_hourly(web, tmp_path, monkeypatch):
    """W5/W12: pruning happens while serving, not only at worker import, and
    it must not run on the request thread."""
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
    assert _wait_for(lambda: not os.path.exists(stale)), \
        "a stale entry must be pruned after the request"
    assert os.path.exists(fresh), "fresh entries must be kept"
    assert _wait_prune_idle()

    # Inside the interval a second stale file must survive (bounded frequency).
    stale2 = _stale_cache_file(cache_dir, "c" * 64 + ".json")
    assert web.get("/api/menu").status_code == 200
    time.sleep(0.2)
    assert os.path.exists(stale2), "pruning must not run every hour-or-not"


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
    assert _wait_prune_idle()
    left = [name for name in os.listdir(cache_dir) if name.endswith(".json")]
    assert len(left) == total - app_module.PRUNE_MAX_REMOVALS


def test_cache_prune_does_not_block_the_request_thread(web, monkeypatch):
    """W12: the first request of the hour must not stat the cache directory."""
    import app as app_module

    started = threading.Event()
    finished = threading.Event()

    def slow_prune():
        started.set()
        time.sleep(0.3)
        finished.set()

    monkeypatch.setattr(app_module, "_prune_cache", slow_prune)
    app_module._PRUNE_STATE["at"] = 0.0
    before = time.time()
    response = web.get("/api/menu")
    elapsed = time.time() - before
    assert response.status_code == 200
    assert elapsed < 0.25, "prune ran on the request thread (%.2fs)" % elapsed
    assert started.wait(2.0), "prune thread never started"
    assert finished.wait(2.0)


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


def test_valid_gzip_and_deflate_bodies_still_decode():
    from core import fetch as core_fetch

    body = b"hello compressed world"
    assert core_fetch._decode(_FakeResponse(gzip.compress(body), encoding="gzip")) == \
        body.decode()
    assert core_fetch._decode(_FakeResponse(zlib.compress(body), encoding="deflate")) == \
        body.decode()


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

# --------------------------------------------------------------------------- W1 (1.6.13) — huge / colon-form durations


def test_archive_duration_rejects_huge_and_colon_form_values():
    from core import archive

    assert archive._duration("1:" + "9" * 400) is None          # colon form, huge int
    assert archive._duration("1:" + "9" * 6) is None            # 6-digit part, absurd total
    assert archive._duration(10 ** 5000) is None                # huge int metadata
    assert archive._duration("9" * 5000) is None                # huge digit string
    assert archive._duration(25 * 3600) is None                 # > 24 h display cap
    assert archive._duration("1:02:03") == 3723                 # normal values still work
    assert archive._duration(1872) == 1872
    assert archive._duration("0:01:02") == 62


def test_feed_duration_rejects_huge_and_colon_form_values():
    from core import feeds

    def duration(value):
        return feeds.parse(_feed_xml(value))[0]["duration"]

    assert duration("1:" + "9" * 400) is None
    assert duration("1:" + "9" * 6) is None
    assert duration("9" * 5000) is None
    assert duration("25:00:00") is None
    assert duration("1:02:03.500") == 3723


def test_human_duration_handles_huge_ints_and_colon_forms():
    import app as app_module

    assert app_module._human_duration(10 ** 5000) == ""
    assert app_module._human_duration("9" * 5000) == ""
    assert app_module._human_duration("1:" + "9" * 400) == ""
    assert app_module._human_duration(25 * 3600) == ""
    assert app_module._human_duration(3723) == "1:02:03"
    assert app_module._human_duration(0) == "0:00"


def test_item_with_hostile_colon_form_length_is_not_a_500(web, monkeypatch):
    """W1: reproduce the review's 500 through the real /api/item route."""
    import app as app_module

    monkeypatch.setattr(app_module.archive, "audio_files", lambda identifier: [{
        "name": "a.mp3",
        "title": "Show A",
        "url": "https://archive.org/download/pack/a.mp3",
        "length": "1:" + "9" * 400,
    }])
    response = web.get("/api/item/pack-1")
    assert response.status_code == 200
    assert response.get_json()["items"][0]["sub"] == ""


def test_item_with_huge_int_length_is_not_a_500(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.archive, "audio_files", lambda identifier: [{
        "name": "a.mp3",
        "title": "Show B",
        "url": "https://archive.org/download/pack/b.mp3",
        "length": 10 ** 5000,
    }])
    response = web.get("/api/item/pack-1")
    assert response.status_code == 200
    assert response.get_json()["items"][0]["sub"] == ""


# --------------------------------------------------------------------------- W2 — wall-clock bound on body reads


class _SlowDripResponse(object):
    """A body that returns one byte per recv, slower than any per-recv timeout."""

    def __init__(self, delay=0.05):
        self.headers = email.message.Message()
        self.delay = delay
        self.reads = 0

    def read1(self, size):
        self.reads += 1
        time.sleep(self.delay)
        return b"x"


def test_decode_respects_a_wall_clock_deadline():
    from core import fetch as core_fetch

    response = _SlowDripResponse(delay=0.05)
    started = time.monotonic()
    assert core_fetch._decode(response, deadline=started + 0.15) is None
    elapsed = time.monotonic() - started
    # Old behaviour: read() would consume the whole (endless) body.  0.15 s
    # deadline plus one 0.05 s recv is the bound; allow CI slack.
    assert elapsed < 1.0, "deadline was not applied (%.2fs)" % elapsed
    assert response.reads < 20


def test_request_applies_its_timeout_as_a_whole_fetch_budget(monkeypatch):
    from core import fetch as core_fetch

    seen = {}

    class FakeResponse(object):
        headers = email.message.Message()
        body = b"hello"

        def __init__(self):
            self.offset = 0

        def read1(self, size):
            chunk = self.body[self.offset:self.offset + size]
            self.offset += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeOpener(object):
        def open(self, request, timeout=None):
            seen["timeout"] = timeout
            seen["deadline"] = getattr(request, "raveweb_deadline", None)
            return FakeResponse()

    monkeypatch.setattr(core_fetch, "_OPENER", FakeOpener())
    started = time.monotonic()
    assert core_fetch.fetch("https://example.com/feed", timeout=2) == "hello"
    assert seen["timeout"] == 2
    assert seen["deadline"] is not None
    assert started + 1.5 <= seen["deadline"] <= time.monotonic() + 2.1


# --------------------------------------------------------------------------- W13 — redirect hops share the budget


def test_redirect_hops_are_clamped_to_the_remaining_budget(monkeypatch):
    from core import fetch as core_fetch
    import urllib.request

    recorded = {}
    monkeypatch.setattr(core_fetch, "_validate_redirect", lambda url: url)
    monkeypatch.setattr(
        urllib.request.HTTPRedirectHandler, "http_error_302",
        lambda self, req, fp, code, msg, headers: recorded.setdefault(
            "timeout", getattr(req, "timeout", None)),
    )
    handler = core_fetch._SafeRedirectHandler()
    request = urllib.request.Request("https://public.example/start")
    request.timeout = 5
    request.raveweb_deadline = time.monotonic() + 1.0
    handler.http_error_302(request, None, 302, "Found", {})
    assert recorded["timeout"] is not None
    assert 0.5 <= recorded["timeout"] <= 1.05, recorded


def test_redirect_after_the_budget_is_exhausted_is_refused(monkeypatch):
    from core import fetch as core_fetch
    import urllib.request

    monkeypatch.setattr(core_fetch, "_validate_redirect", lambda url: url)
    handler = core_fetch._SafeRedirectHandler()
    request = urllib.request.Request("https://public.example/start")
    request.raveweb_deadline = time.monotonic() - 1
    with pytest.raises(urllib.error.HTTPError):
        handler.http_error_302(request, None, 302, "Found", {})
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {},
                                 "https://public.example/next")


# --------------------------------------------------------------------------- W4 — rave catalogue counts


def test_malformed_rave_catalog_counts_do_not_break_the_section(web, tmp_path, monkeypatch):
    import app as app_module
    from core import raves

    catalog = tmp_path / "rave-catalog.json"
    catalog.write_text(json.dumps([{
        "name": "Test Rave",
        "archive_query": "test",
        "archive_count": "1,200",
        "mixcloud_count": None,
    }]), encoding="utf-8")
    monkeypatch.setattr(raves, "CATALOG", str(catalog))
    registry = raves.registry()
    assert registry[0]["archive_count"] == 0
    assert registry[0]["mixcloud_count"] == 0
    assert web.get("/api/raves").status_code == 200


# --------------------------------------------------------------------------- W5 — cold /api/latest fan-out


def test_latest_merge_runs_every_loader_concurrently(web, monkeypatch):
    from core import latest

    feeds = [{"id": "f%d" % index, "name": "Feed %d" % index, "url": "https://f/rss"}
             for index in range(5)]
    barrier = threading.Barrier(7)  # 5 feeds + Archive + DNBRADIO
    reached = []

    def feed_items(feed_id, limit=6):
        barrier.wait(timeout=3)
        reached.append(feed_id)
        return []

    def packs(rows=20):
        barrier.wait(timeout=3)
        reached.append("packs")
        return []

    def dnb(limit=12):
        barrier.wait(timeout=3)
        reached.append("dnb")
        return []

    monkeypatch.setattr(latest.feeds, "registry", lambda: feeds)
    monkeypatch.setattr(latest.feeds, "episodes", feed_items)
    monkeypatch.setattr(latest, "recent_packs", packs)
    monkeypatch.setattr(latest, "recent_dnb", dnb)
    assert latest.merge(limit=10) == []
    assert not barrier.broken, "a loader waited for another (MERGE_WORKERS too small)"
    assert len(reached) == 7


# --------------------------------------------------------------------------- W6 — entity scan covers the document


def test_entity_declaration_after_64k_is_still_refused():
    from core import feeds

    padding = "A" * 70000
    document = (
        "<?xml version='1.0'?><!-- " + padding + " -->"
        "<!DOCTYPE rss [<!ENTITY a 'EXPANDED-OK'>]>"
        "<rss><channel><item><title>&a;</title>"
        "<enclosure url='https://cdn.example/x.mp3'/></item></channel></rss>"
    )
    assert feeds.parse(document) == []


# --------------------------------------------------------------------------- W7 — feed media URLs are http(s) only


def test_feed_media_urls_must_be_http_or_https():
    from core import feeds

    def document(url):
        return ("<?xml version='1.0'?><rss><channel><item><title>T</title>"
                "<enclosure url='%s'/></item></channel></rss>" % url)

    assert feeds.parse(document("javascript:alert(1)")) == []
    assert feeds.parse(document("file:///etc/passwd")) == []
    assert feeds.parse(document("/podcast/x.mp3")) == []
    episodes = feeds.parse(document("https://cdn.example/x.mp3"))
    assert len(episodes) == 1 and episodes[0]["url"] == "https://cdn.example/x.mp3"


# --------------------------------------------------------------------------- W8 — cache entry hardening


def test_cache_entry_without_value_is_dropped(tmp_path, monkeypatch):
    from core import cache

    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    key = "test:missing-value"
    path = cache.path_for(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"v": cache.VERSION, "ts": time.time()}, handle)
    assert cache.get(key, 60) is cache.MISSING
    assert not os.path.exists(path), "a valueless entry must be deleted, not re-read"


def test_oversized_cache_entry_is_dropped(tmp_path, monkeypatch):
    from core import cache

    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cache, "MAX_ENTRY_BYTES", 32)
    key = "test:oversized"
    path = cache.path_for(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"v": cache.VERSION, "ts": time.time(), "value": "x" * 100}, handle)
    assert cache.get(key, 60) is cache.MISSING
    assert not os.path.exists(path)

    monkeypatch.setattr(cache, "MAX_ENTRY_BYTES", 4096)
    assert cache.put(key, "kept") is True
    assert cache.get(key, 60) == "kept"


# --------------------------------------------------------------------------- W9 — DNB Radio dates


def test_dnbradio_without_a_date_separator_does_not_duplicate_the_title():
    from core import dnbradio

    body_without = '<a href="/listen?mix=7" title="Some Show">x</a>'
    parsed = dnbradio.parse_recordings(body_without)
    assert parsed[0]["title"] == "Some Show"
    assert parsed[0]["date"] == ""

    body_with = '<a href="/listen?mix=8" title="2026-10-29 : DJ - Show">x</a>'
    parsed = dnbradio.parse_recordings(body_with)
    assert parsed[0]["title"] == "DJ - Show"
    assert parsed[0]["date"] == "2026-10-29"


# --------------------------------------------------------------------------- W10 — legacy parity


def test_legacy_url_path_rejects_traversal_and_empty_segments():
    from core import legacy

    assert legacy._url_path("..") == ""
    assert legacy._url_path("a/../b") == ""
    assert legacy._url_path("a//b") == ""
    assert legacy._url_path("/") == ""
    assert legacy._url_path("Pack 1/Tape 2.mp3") == "Pack%201/Tape%202.mp3"


def test_legacy_packs_dedupe_urls_and_skip_bad_paths(monkeypatch):
    from core import legacy

    page = (
        '<a href="/music/PackOne/Tape1.mp3">a</a>'
        '<a href="/music/PackOne/Tape1.mp3">b</a>'
        '<a href="/music/../Tape2.mp3">c</a>'
    )
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)
    packs = legacy.packs()
    urls = [track["url"] for pack in packs for track in pack["tracks"]]
    assert urls == ["https://www.ravetapepacks.com/music/PackOne/Tape1.mp3"]


def test_legacy_playable_does_not_double_the_header_suffix():
    from core import legacy

    plain = "https://www.ravetapepacks.com/music/x.mp3"
    with_suffix = legacy.playable(plain)
    assert with_suffix.endswith(legacy.REFERER_SUFFIX)
    assert legacy.playable(with_suffix) == with_suffix


# --------------------------------------------------------------------------- W11 — /api/ukg cleaning


def test_ukg_rows_are_cleaned_and_bounded(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.mixcloud, "search", lambda *a, **k: [])
    monkeypatch.setattr(app_module.archive, "search", lambda *a, **k: ([{
        "identifier": "pack-1",
        "title": ["List", "Title"],
    }, {
        "identifier": "pack-2",
        "title": "x" * 5000,
    }], 2))
    response = web.get("/api/ukg")
    assert response.status_code == 200
    items = response.get_json()["items"]
    labels = [item["label"] for item in items]
    assert labels[0] == "pack-1"  # list title falls back to the identifier
    assert len(labels[1]) == app_module.MAX_TITLE
    assert all(isinstance(item["fav"]["title"], str) for item in items)


# --------------------------------------------------------------------------- W14 — documented open-proxy default


def test_readme_documents_the_stream_proxy_default():
    readme = _read("README.md")
    lowered = readme.lower()
    assert "RAVEWEB_STREAM_HOSTS" in readme
    assert "open proxy" in lowered and "lan-only" in lowered
