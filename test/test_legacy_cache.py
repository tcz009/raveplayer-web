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


def _category_page(*posts):
    return "".join(
        '<h1 class="entry-title"><a href="?p=%s" rel="bookmark">%s</a></h1>'
        '<a href="?p=%s" rel="bookmark"><time>date</time></a>' % (pid, name, pid)
        for pid, name in posts)


def test_category_page_is_the_only_fetch_on_a_cold_category_view(monkeypatch):
    page = _category_page(("899", "Dreamscape 7"), ("879", "Dreamscape 6"))
    urls = []

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        urls.append(url)
        return page

    monkeypatch.setattr(legacy.http, "fetch", fake_fetch)
    posts = legacy.category("331")

    assert [post["id"] for post in posts] == ["899", "879"]
    assert urls == [legacy.BASE + "?cat=331"]
    assert posts[0]["name"] == "Dreamscape 7"


def test_category_rejects_non_numeric_ids_without_fetching(monkeypatch):
    calls = []
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: calls.append(a) or "")
    assert legacy.category("../../etc/passwd") == []
    assert legacy.category("") == []
    assert legacy.category("five") == []
    assert calls == []


def test_category_follows_older_posts_and_collects_every_page(monkeypatch):
    page_one = _category_page(("899", "Dreamscape 7")) + (
        '<div class="nav-previous"><a href="?cat=5&#038;paged=2">'
        "Older posts</a></div>")
    page_two = _category_page(("42", "Dreamscape 3"))
    pages = {legacy.BASE + "?cat=5": page_one,
             legacy.BASE + "?cat=5&paged=2": page_two}
    urls = []

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        urls.append(url)
        return pages.get(url)

    monkeypatch.setattr(legacy.http, "fetch", fake_fetch)
    posts = legacy.category("5")

    assert [post["id"] for post in posts] == ["899", "42"]
    assert urls == [legacy.BASE + "?cat=5", legacy.BASE + "?cat=5&paged=2"]


def test_category_pagination_is_bounded(monkeypatch):
    calls = []

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        calls.append(url)
        number = int(url.rsplit("paged=", 1)[-1]) if "paged=" in url else 1
        return ('<a href="?p=%d" rel="bookmark">Pack %d</a>'
                '<div class="nav-previous"><a href="?cat=5&amp;paged=%d">'
                "Older posts</a></div>" % (number, number, number + 1))

    monkeypatch.setattr(legacy.http, "fetch", fake_fetch)
    monkeypatch.setattr(legacy, "MAX_CATEGORY_PAGES", 3)
    posts = legacy.category("5")

    assert len(calls) == 3
    assert [post["id"] for post in posts] == ["1", "2", "3"]


def test_post_page_tracks_are_parsed_and_cached(monkeypatch):
    page = (
        '<html><head><title>Dreamscape 7 | Rave Tape Packs</title></head><body>'
        '<h1 class="entry-title">Dreamscape 7</h1>'
        '<audio src="https://www.ravetapepacks.com/music/Dreamscape7/Charlie_Hall.mp3"></audio>'
        '<audio src="https://www.ravetapepacks.com/music/Dreamscape7/Clarkee.mp3"></audio>'
        '</body></html>')
    calls = []
    original = legacy._post_from_html
    monkeypatch.setattr(legacy, "_post_from_html",
                        lambda body, post_id: calls.append(post_id) or original(body, post_id))
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)

    first = legacy.post("899")
    second = legacy.post("899")

    assert first == second
    assert len(calls) == 1
    assert first["name"] == "Dreamscape 7"
    assert [track["name"] for track in first["tracks"]] == ["Charlie Hall", "Clarkee"]
    assert first["tracks"][0]["url"].endswith(
        "/music/Dreamscape7/Charlie_Hall.mp3")


def test_post_rejects_non_numeric_ids_without_fetching(monkeypatch):
    calls = []
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: calls.append(a) or "")
    assert legacy.post("../899") is None
    assert legacy.post("") is None
    assert legacy.post("abc") is None
    assert calls == []


def test_pack_resolves_from_the_homepage_index(monkeypatch):
    entry = {"key": "HomePack", "name": "Home Pack", "tracks": []}
    monkeypatch.setattr(legacy, "packs", lambda: [entry])

    assert legacy.pack("HomePack") is entry
    assert legacy.pack("Missing") is None
    assert legacy.pack("") is None


def test_literal_entity_in_a_filename_is_not_decoded_twice(monkeypatch):
    page = ('<a href="/music/EntityPack/Literal_%26amp%3B_Name.mp3">x</a>'
            '<a href="/music/Entity%20Pack/Percent_%2520_Literal.mp3">y</a>')
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)

    packs = {pack["key"]: pack for pack in legacy.packs()}

    assert packs["EntityPack"]["tracks"][0]["name"] == "Literal &amp; Name"
    # %2520 decodes once to a literal "%20", not to a space.
    assert packs["Entity Pack"]["tracks"][0]["name"] == "Percent %20 Literal"
