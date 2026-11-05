# -*- coding: utf-8 -*-
"""Performance regressions for the web build (perf/overhaul 1.6.52).

Locks the behaviours this pass relies on: parsed pages persist per exact body,
the RTP category title never adds a homepage fetch, a cold Moxser resolve does
not load the sitemap first, SQLite session checks stay to one connection, and
versioned static assets are cacheable.
"""

import os
import time

import pytest

import app as app_module
from core import audit, cache, fetch as fetch_mod, legacy, moxser, toronto

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def fixture(name):
    """Read a provider fixture (Toronto pages live under ``fixtures/toronto``)."""
    for folder in (FIXTURES, os.path.join(FIXTURES, "toronto")):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()
    raise AssertionError("missing fixture %s" % name)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    moxser.clear_memory()
    yield
    cache.clear_memory()
    moxser.clear_memory()


# --------------------------------------------------------------------- parsed pages


def test_cache_parsed_parses_once_per_body():
    calls = []

    def parser(body):
        calls.append(body)
        return [{"value": body}]

    assert cache.parsed("perf:parsed", 60, "page", parser) == [{"value": "page"}]
    assert cache.parsed("perf:parsed", 60, "page", parser) == [{"value": "page"}]
    assert calls == ["page"]


def test_cache_parsed_reparses_when_the_body_changes():
    assert cache.parsed("perf:parsed-change", 60, "one", lambda b: [b]) == ["one"]
    assert cache.parsed("perf:parsed-change", 60, "two", lambda b: [b]) == ["two"]


def test_toronto_genre_parse_runs_once_per_page(monkeypatch):
    page = fixture("jungle.html")
    calls = []
    original = toronto.parse_page

    def counting(body):
        calls.append(body)
        return original(body)

    monkeypatch.setattr(toronto, "parse_page", counting)
    monkeypatch.setattr(toronto, "_get", lambda url, ttl=None: page)
    first = toronto.genre_items("jungle")
    second = toronto.genre_items("jungle")
    assert first == second and first
    assert len(calls) == 1


# ---------------------------------------------------------------- RTP category title


def test_rtp_category_name_never_fetches_the_homepage(monkeypatch):
    """A cold category link is one ?cat= page, not the homepage as well."""
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("category title must not fetch")

    monkeypatch.setattr(legacy.http, "fetch", forbidden)
    assert app_module._rtp_category_name("events", "331") == ""
    assert calls == []


def test_rtp_category_name_uses_the_cached_parse(monkeypatch):
    cache.put(legacy.RTP_CATEGORIES_KEY,
              {"fingerprint": "x", "value": {
                  "events": [{"id": "331", "name": "Dreamscape"}],
                  "djs": [], "mcs": [], "years": []}})
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must use the cache")))
    assert app_module._rtp_category_name("events", "331") == "Dreamscape"


def test_cached_body_returns_none_without_a_request(monkeypatch):
    monkeypatch.setattr(fetch_mod, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not fetch")))
    assert fetch_mod.cached_body("https://example.com/none", 60) is None


# ------------------------------------------------------------------- Moxser detail


def test_moxser_detail_derives_the_page_url_without_loading_sitemaps(monkeypatch):
    calls = []

    def page(url, ttl):
        calls.append(url)
        return ('<h1>Bench Mix</h1>'
                "<script>window.x={path:'https://cdn.example/bench.mp3'}</script>")

    monkeypatch.setattr(moxser, "_get", page)
    monkeypatch.setattr(moxser, "_sitemap_urls", lambda: (_ for _ in ()).throw(
        AssertionError("must not load the sitemap index")))
    moxser.clear_memory()
    entry = moxser.detail("bench-mix")
    assert entry["url"] == "https://cdn.example/bench.mp3"
    assert calls == [moxser.MIX_PREFIX + "bench-mix"]


def test_moxser_detail_rejects_a_hostile_slug_without_a_request(monkeypatch):
    monkeypatch.setattr(moxser, "_get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not fetch")))
    assert moxser.detail("../../etc/passwd") is None


# --------------------------------------------------------------------- audit store


def test_session_check_updates_last_seen_only_after_the_window(tmp_path):
    path = str(tmp_path / "auth.sqlite3")
    audit.configure(path)
    sid = audit.create_session("owner", "admin", "127.0.0.1", "pytest", 3600)
    rows = audit._read("SELECT last_seen FROM sessions WHERE id=?", (sid,))
    first_seen = rows[0]["last_seen"]
    time.sleep(0.05)
    active, blocked = audit.session_check(sid, "owner", "127.0.0.1", "pytest",
                                          touch_after=30)
    assert active is True and blocked is False
    rows = audit._read("SELECT last_seen FROM sessions WHERE id=?", (sid,))
    assert rows[0]["last_seen"] == first_seen  # inside the window: no write
    active, blocked = audit.session_check(sid, "owner", "127.0.0.1", "pytest",
                                          touch_after=0)
    assert active is True
    rows = audit._read("SELECT last_seen FROM sessions WHERE id=?", (sid,))
    assert rows[0]["last_seen"] > first_seen


def test_session_check_reports_blocks_and_revocation(tmp_path):
    path = str(tmp_path / "auth.sqlite3")
    audit.configure(path)
    sid = audit.create_session("friends", "viewer", "10.0.0.1", "pytest", 3600)
    assert audit.session_check(sid, "friends", "10.0.0.1") == (True, False)
    audit.add_block(identity="friends", seconds=60)
    assert audit.session_check(sid, "friends", "10.0.0.1") == (True, True)
    audit.revoke_session(sid)
    assert audit.session_check(sid, "friends", "10.0.0.1") == (False, False)


# ------------------------------------------------------------------ static caching


def test_versioned_static_assets_are_immutable(web):
    response = web.get("/static/app.js?v=%s" % app_module.APP_VERSION)
    assert response.status_code == 200
    assert "immutable" in response.headers["Cache-Control"]
    assert "max-age=31536000" in response.headers["Cache-Control"]


def test_unversioned_static_assets_stay_short_lived(web):
    response = web.get("/static/art/icon.png")
    assert response.status_code == 200
    assert "max-age=300" in response.headers["Cache-Control"]


def test_api_responses_stay_no_store(web):
    response = web.get("/api/menu")
    assert response.headers["Cache-Control"] == "no-store"
