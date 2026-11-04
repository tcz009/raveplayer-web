# -*- coding: utf-8 -*-
"""RaveTapePacks cache/parse behaviour (no network).

The live pages are replayed in ``scripts/bench_sources.py``; these tests pin the
two properties the performance pass relies on:

* a parsed page is reused for the same body (and re-parsed when the body
  changes, so fixtures/mocks cannot serve a stale parse);
* a category pack detail no longer fetches/parses the homepage first.
"""

import pytest

from core import legacy

PACKS_KEY = legacy.RTP_PACKS_KEY


def _page(pack, track="Tape_1"):
    return '<a href="/music/%s/%s.mp3">%s</a>' % (pack, track, track)


def test_packs_parse_once_for_the_same_body(monkeypatch):
    page = _page("ParseOncePack")
    calls = []
    original = legacy._packs_from_html
    monkeypatch.setattr(legacy, "_packs_from_html",
                        lambda body: calls.append(body) or original(body))
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)

    first = legacy.packs()
    second = legacy.packs()

    assert [pack["key"] for pack in first] == ["ParseOncePack"]
    assert first == second
    assert len(calls) == 1


def test_packs_reparse_when_the_body_changes(monkeypatch):
    pages = [_page("BodyPackOne", "One"), _page("BodyPackTwo", "Two")]
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: pages.pop(0))

    assert [pack["key"] for pack in legacy.packs()] == ["BodyPackOne"]
    assert [pack["key"] for pack in legacy.packs()] == ["BodyPackTwo"]


def test_packs_fetch_uses_the_browser_headers_the_site_requires(monkeypatch):
    seen = []

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        seen.append((url, dict(headers or {}), ttl))
        return _page("HeaderPack")

    monkeypatch.setattr(legacy.http, "fetch", fake_fetch)
    legacy.packs()

    assert seen and seen[0][0] == legacy.BASE
    assert seen[0][1].get("User-Agent") == legacy.BROWSER_UA
    assert seen[0][1].get("Accept", "").startswith("text/html")
    assert seen[0][2] == legacy.PACKS_TTL


def test_category_page_is_the_only_fetch_on_a_cold_category_view(monkeypatch):
    page = _page("CategoryOnlyPack")
    urls = []

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        urls.append(url)
        return page

    monkeypatch.setattr(legacy.http, "fetch", fake_fetch)
    packs = legacy.category_packs("events", "331")

    assert [pack["key"] for pack in packs] == ["CategoryOnlyPack"]
    assert urls == [legacy.BASE + "?cat=331"]


def test_category_packs_reject_non_numeric_ids_without_fetching(monkeypatch):
    calls = []
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: calls.append(a) or "")
    assert legacy.category_packs("events", "../../etc/passwd") == []
    assert legacy.category_packs("nope", "1") == []
    assert calls == []


def test_pack_prefers_the_linked_category_and_skips_the_homepage(monkeypatch):
    entry = {"key": "CatPack", "name": "Cat Pack", "tracks": []}
    monkeypatch.setattr(legacy, "category_packs", lambda kind, cat_id: [entry])
    monkeypatch.setattr(legacy, "packs", lambda: pytest.fail("homepage parsed"))

    assert legacy.pack("CatPack", kind="events", cat_id="331") is entry


def test_pack_falls_back_to_the_homepage_index(monkeypatch):
    entry = {"key": "HomePack", "name": "Home Pack", "tracks": []}
    monkeypatch.setattr(legacy, "category_packs", lambda kind, cat_id: [])
    monkeypatch.setattr(legacy, "packs", lambda: [entry])

    assert legacy.pack("HomePack", kind="events", cat_id="331") is entry
    assert legacy.pack("Missing") is None
    assert legacy.pack("") is None


def test_category_packs_are_cached_and_reused(monkeypatch):
    page = _page("CachedCatPack")
    calls = []
    original = legacy._packs_from_html
    monkeypatch.setattr(legacy, "_packs_from_html",
                        lambda body: calls.append(body) or original(body))
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)

    first = legacy.category_packs("djs", "999")
    second = legacy.category_packs("djs", "999")

    assert first == second
    assert len(calls) == 1


def test_literal_entity_in_a_filename_is_not_decoded_twice(monkeypatch):
    page = ('<a href="/music/EntityPack/Literal_%26amp%3B_Name.mp3">x</a>'
            '<a href="/music/Entity%20Pack/Percent_%2520_Literal.mp3">y</a>')
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)

    packs = {pack["key"]: pack for pack in legacy.packs()}

    assert packs["EntityPack"]["tracks"][0]["name"] == "Literal &amp; Name"
    # %2520 decodes once to a literal "%20", not to a space.
    assert packs["Entity Pack"]["tracks"][0]["name"] == "Percent %20 Literal"
