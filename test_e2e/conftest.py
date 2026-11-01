# -*- coding: utf-8 -*-
"""Fixtures for the headless-browser smoke tests (``pytest test_e2e``).

These tests run the real Flask app over real HTTP on a random localhost port
(werkzeug), with a temporary ``RAVEWEB_DATA`` directory.  Provider calls are
replaced with tiny deterministic stubs and every non-localhost request the
browser makes is intercepted (widget iframes get a local stub document,
images/media are aborted), so the suite never depends on the live Internet
Archive, Mixcloud or SoundCloud network.

The browser tests are deliberately NOT part of the offline suite
(``pytest test``).  If Playwright or its Chromium build is missing the tests
skip with a clear message; set ``RAVEWEB_REQUIRE_BROWSER=1`` (CI does) to make
that skip a failure instead, so a broken CI browser install cannot pass
silently.
"""

import datetime
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Point the app at throwaway data before it is imported, so even collection
# never touches a real favourites/cache directory.
_BOOT_DATA = tempfile.mkdtemp(prefix="raveweb-e2e-boot-")
os.environ.setdefault("RAVEWEB_DATA", _BOOT_DATA)
os.environ.setdefault("RAVEPLAYER_CACHE_DIR", os.path.join(_BOOT_DATA, "cache"))
os.environ.setdefault("RAVEPLAYER_FAV_DIR", os.path.join(_BOOT_DATA, "favs"))
os.environ.pop("SOUNDCLOUD_CLIENT_ID", None)  # keep the optional API search off
os.environ.pop("RAVEWEB_VERSION", None)  # /healthz reports the VERSION file
# Authentication is mandatory in 1.6.20; the fixture logs each browser page in.
os.environ.pop("RAVEWEB_PASSWORD_HASH", None)  # deterministic test credential
os.environ["SECRET_KEY"] = "e2e-secret-key"
os.environ["RAVEWEB_PASSWORD"] = "test-password"
os.environ["RAVEWEB_ENV"] = "test"

import app as app_module  # noqa: E402  (import intentionally after the env setup)

REQUIRE_BROWSER = os.environ.get("RAVEWEB_REQUIRE_BROWSER") == "1"
NO_SANDBOX = os.environ.get("RAVEWEB_PLAYWRIGHT_NO_SANDBOX") == "1"
CHROMIUM_EXECUTABLE = os.environ.get("RAVEWEB_CHROMIUM_EXECUTABLE") or None

WIDGET_STUB = (
    "<!doctype html><html><head><title>stub widget</title></head>"
    "<body><p>stub widget</p></body></html>"
)


# --------------------------------------------------------------------------- stubs


def _fake_latest_merge(limit=60):
    """A fixed Latest list so boot is fast and deterministic."""
    return [
        {"kind": "pack", "title": "Boot Pack", "identifier": "boot-pack",
         "source": "Rave Tape Packs", "when": datetime.datetime(2026, 1, 2)},
        {"kind": "episode", "title": "Boot Episode", "url": "https://cdn.example/episode.mp3",
         "source": "Test Feed", "when": None},
    ]


def _fake_archive_search(query, page=1, rows=50, sort="downloads desc"):
    """Archive search stub: enough rows for /api/search without any network."""
    docs = [
        {"identifier": "dreamscape-10", "title": "Dreamscape 10", "year": "1993"},
        {"identifier": "dreamscape-11", "title": "Dreamscape 11", "year": "1994"},
    ]
    return docs, 25


def _no_mixcloud_search(*args, **kwargs):
    """Keep /api/search offline: the live route also appends Mixcloud sets."""
    return []


def _fake_audio_files(identifier):
    """Archive item tracks for /api/item/<id> (Surprise Me opening a pack)."""
    return [{
        "name": "boot-track.mp3", "title": "Boot Track",
        "url": "https://archive.org/download/boot-pack/boot-track.mp3", "length": 187,
    }]


def _fake_feed_episodes(feed_id):
    """Feed episodes for /api/podcast/<id> (Surprise Me opening a show)."""
    return [{
        "title": "Boot Episode", "url": "https://cdn.example/boot-episode.mp3",
        "published": "2026-01-02", "duration": 187,
    }]


def _no_outbound_request(*args, **kwargs):
    """Any unmocked provider fetch during a browser test is a test bug."""
    raise AssertionError("browser smoke tests must not hit the network")


class _FakeDownloadResponse(object):
    """A slow, chunked audio body so the progress panel stays observable."""

    status = 200
    BODY = b"ID3" + b"\xff" * (2 * 1024 * 1024 - 3)
    CHUNK = 64 * 1024
    DELAY = 0.025  # ~0.8 s total: long enough for progress/cancel in the UI

    def __init__(self, body=None):
        self._body = body if body is not None else self.BODY
        self._offset = 0

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return "audio/mpeg"
        if lowered == "content-length":
            return str(len(self._body))
        return None

    def read(self, size=-1):
        if self._offset >= len(self._body):
            return b""
        time.sleep(self.DELAY)
        step = min(size if size and size > 0 else self.CHUNK, self.CHUNK)
        chunk = self._body[self._offset:self._offset + step]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeDownloadConnection(object):
    def close(self):
        pass


def _fake_open_stream(url, headers, deadline):
    return (_FakeDownloadConnection(), _FakeDownloadResponse(), url)


# --------------------------------------------------------------------------- server


@pytest.fixture(scope="session")
def live_server():
    """The real Flask app on a random localhost port with isolated data."""
    from core import fetch as core_fetch
    from werkzeug.serving import make_server

    data_dir = tempfile.mkdtemp(prefix="raveweb-e2e-data-")
    patcher = pytest.MonkeyPatch()
    patcher.setenv("RAVEWEB_DATA", data_dir)
    patcher.setenv("RAVEPLAYER_CACHE_DIR", os.path.join(data_dir, "cache"))
    patcher.setenv("RAVEPLAYER_FAV_DIR", os.path.join(data_dir, "favs"))
    patcher.delenv("SOUNDCLOUD_CLIENT_ID", raising=False)
    patcher.setattr(app_module, "DATA_DIR", data_dir)
    patcher.setattr(app_module, "_DATA_PROBE", {"at": 0.0, "ok": True})
    patcher.setattr(app_module.latest, "merge", _fake_latest_merge)
    patcher.setattr(app_module.archive, "search", _fake_archive_search)
    patcher.setattr(app_module.archive, "audio_files", _fake_audio_files)
    patcher.setattr(app_module.feeds, "episodes", _fake_feed_episodes)
    patcher.setattr(app_module.mixcloud, "search", _no_mixcloud_search)
    # Downloads reuses the stream proxy's opener; give it a local audio body so
    # the e2e suite never leaves the box.
    patcher.setattr(app_module, "_open_stream", _fake_open_stream)
    # Belt and braces: every provider HTTP call goes through core.fetch, so a
    # stub we missed fails loudly instead of quietly going online.
    patcher.setattr(core_fetch, "_request", _no_outbound_request)

    server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:%d" % server.server_port
    try:
        yield SimpleNamespace(url=base_url, data_dir=data_dir)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        patcher.undo()


# --------------------------------------------------------------------------- browser


def _skip_or_fail(message):
    if REQUIRE_BROWSER:
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def playwright_api():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        _skip_or_fail(
            "Python Playwright is not installed (%s). "
            "Run: pip install -r requirements-browser.txt" % exc
        )
    manager = sync_playwright().start()
    try:
        yield manager
    finally:
        manager.stop()


@pytest.fixture(scope="session")
def chromium(playwright_api):
    launch = {"headless": True, "args": []}
    if NO_SANDBOX:  # some local sandboxes refuse Chromium's own sandbox
        launch["args"].append("--no-sandbox")
    if CHROMIUM_EXECUTABLE:  # e.g. use an already-installed Chrome locally
        launch["executable_path"] = CHROMIUM_EXECUTABLE
    try:
        browser = playwright_api.chromium.launch(**launch)
    except Exception as exc:  # pragma: no cover - depends on the environment
        _skip_or_fail(
            "Playwright Chromium could not start (%s). "
            "Run: python -m playwright install chromium" % exc
        )
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture()
def browser_page(chromium, live_server):
    """A fresh page on the app, offline: no external request leaves the box."""
    context = chromium.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def handle_route(route):
        request = route.request
        if request.url.startswith(live_server.url):
            route.continue_()
        elif request.resource_type == "document":
            # A framed widget host: a local stub document, still subject to
            # the page's CSP exactly like the real iframe request would be.
            route.fulfill(status=200, content_type="text/html", body=WIDGET_STUB)
        else:
            # External artwork/media is irrelevant to these tests.
            route.abort()

    page.route("**/*", handle_route)
    page.goto(live_server.url + "/", wait_until="domcontentloaded")
    if page.locator("#login-form").count():
        page.fill("#password", "test-password")
        page.click("#login-submit")
    page.wait_for_selector("#nav button")
    page.wait_for_function(
        "() => document.querySelectorAll('#list .row:not(.skeleton)').length > 0"
    )
    # CSP violations are exposed as DOM events (more reliable than scraping
    # console text), so install the collector before any iframe is created.
    page.evaluate(
        """() => {
            window.__cspViolations = [];
            document.addEventListener("securitypolicyviolation", (event) => {
                window.__cspViolations.push({
                    blockedURI: event.blockedURI || "",
                    directive: event.violatedDirective || "",
                });
            });
        }"""
    )
    try:
        yield SimpleNamespace(
            page=page, base_url=live_server.url, data_dir=live_server.data_dir,
            page_errors=page_errors,
        )
    finally:
        context.close()
