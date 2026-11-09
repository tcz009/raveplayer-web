# -*- coding: utf-8 -*-
"""Toronto Rave Mixtape Archive source (1.6.38) — offline fixtures only."""

import os
import re
import struct

import app as app_module
from core import toronto

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "toronto")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def test_nav_fixture_lists_every_genre_page():
    page = _fixture("home.html")
    hrefs = set(re.findall(r'href="(/(?:jungle|house|studio|xstatic)\.html)"', page))
    assert hrefs == {"/jungle.html", "/house.html", "/studio.html", "/xstatic.html"}
    labels = set(re.findall(r">([^<>]+)</a>", page))
    assert {"Jungle &amp; Hardcore", "House &amp; Techno", "Studio", "X-Static"} <= labels


def test_parse_each_genre_page_keeps_direct_mp3s():
    expected = {"jungle": "187- Live at Syrous",
                "house": "A Place Called Bliss",
                "studio": "A Man Called Warwick",
                "xstatic": "Ace - Hardcore"}
    for genre, prefix in expected.items():
        rows = toronto.parse_page(_fixture(genre + ".html"))
        assert rows, genre
        assert rows[0]["title"].startswith(prefix)
        assert rows[0]["url"].startswith("https://torontoravemixtapearchive.com/files/")
        assert rows[0]["url"].endswith(".mp3")
        assert rows[0]["duration"]
        assert rows[0]["metadata"]
        for row in rows:
            assert row["url"].startswith("https://torontoravemixtapearchive.com/files/")
            assert "/files/mixtape/" in row["url"] or "/files/xstatic/" in row["url"]
            assert not row.get("external")


def test_parse_extracts_date_venue_and_xstatic_path():
    rows = toronto.parse_page(_fixture("jungle.html"))
    first = rows[0]
    assert first["date"] == "1993-09-04"
    assert first["venue"] == "Syrous The Next Level"
    house = toronto.parse_page(_fixture("house.html"))
    assert house[1]["date"] == "1993-12-15"
    assert house[1]["venue"] == "Destiny 3"
    xstatic = toronto.parse_page(_fixture("xstatic.html"))
    assert "/files/xstatic/" in xstatic[0]["url"]


def test_parse_drops_non_track_links_and_handles_junk():
    page = '''<a href="/files/mixtape/good.mp3" class="track-dl" download="Good"><div class="header">Good</div></a>
    <a href="/files/mixtape/page.html" class="track-dl" download="Not audio">bad</a>
    <a href="https://example.com/track.mp3" class="track-dl" download="Wrong host">bad</a>
    <a href="/files/xstatic/direct.mp3" class="track-dl">No download attr</a>'''
    rows = toronto.parse_page(page)
    assert [row["title"] for row in rows] == ["Good", "direct"]
    assert toronto.parse_page("") == []
    assert toronto.parse_page(None) == []


def test_genre_items_fetches_once_per_call_with_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(toronto, "_throttle", lambda: None)
    monkeypatch.setattr(toronto.fetch, "fetch", lambda url, timeout, ttl: (
        calls.append((url, timeout, ttl)) or _fixture("jungle.html")))
    rows = toronto.genre_items("jungle", limit=3)
    assert len(rows) == 3
    assert calls[0][0].endswith("/jungle.html")
    assert calls[0][2] == toronto.PAGE_TTL
    assert toronto.genre_items("nope") == []
    assert toronto.GENRE_BY_ID["jungle"]["label"] == "Jungle & Hardcore"


def test_search_filters_all_genres(monkeypatch):
    rows = toronto.parse_page(_fixture("jungle.html"))
    monkeypatch.setattr(toronto, "_genre_rows",
                        lambda genre: rows if genre == "jungle" else [])
    assert toronto.search("syrous")[0]["venue"] == "Syrous The Next Level"
    assert toronto.search("nothing-matches") == []
    assert toronto.search("") == []


def test_toronto_hub_and_genre_api(web, monkeypatch):
    row = toronto.parse_page(_fixture("jungle.html"))[0]
    monkeypatch.setattr(app_module.toronto, "genre_items", lambda genre, limit=60: [row])
    hub = web.get("/api/toronto").get_json()
    assert hub["title"] == "Toronto Rave Mixtape Archive"
    assert [item["label"] for item in hub["items"]] == [
        "Jungle & Hardcore", "House & Techno", "Studio", "X-Static"]
    assert hub["items"][0]["open"] == "/api/toronto?genre=jungle"
    assert all(item["art"] == "/static/art/toronto.png" for item in hub["items"])

    payload = web.get("/api/toronto?genre=jungle").get_json()
    assert payload["title"].endswith("Jungle & Hardcore")
    item = payload["items"][0]
    assert item["play"] == row["url"]
    assert item["download"] == {"source": "toronto"}
    assert item["date"] == "1993-09-04"
    assert item["venue"] == "Syrous The Next Level"
    assert "external" not in item
    assert item["art"] == "/static/art/toronto.png"
    assert web.get("/api/toronto?genre=nope").status_code == 404

    menu = web.get("/api/menu").get_json()
    toronto_section = next(s for s in menu["sections"] if s["id"] == "toronto")
    assert toronto_section["icon"] == "/static/art/toronto.png"
    packs = web.get("/api/rail/packs").get_json()
    assert "Toronto Rave Mixtape Archive" in [item["label"] for item in packs["items"]]
    toronto_row = next(item for item in packs["items"]
                       if item["label"] == "Toronto Rave Mixtape Archive")
    assert toronto_row["art"] == "/static/art/toronto.png"
    paths = {item["open"] for item in packs["items"]}
    assert "/api/toronto" in paths


def test_toronto_search_api(web, monkeypatch):
    row = toronto.parse_page(_fixture("house.html"))[1]
    monkeypatch.setattr(app_module.toronto, "search", lambda query, limit=30: [row])
    payload = web.get("/api/toronto?q=destiny").get_json()
    assert payload["title"].endswith("destiny")
    assert payload["items"][0]["venue"] == "Destiny 3"


def test_toronto_is_a_direct_download_source():
    url = "https://torontoravemixtapearchive.com/files/mixtape/A - Set.mp3"
    assert app_module._download_allowed("toronto", url)[0]
    assert app_module._download_allowed(
        "toronto", "https://torontoravemixtapearchive.com/files/xstatic/A.mp3")[0]
    assert not app_module._download_allowed(
        "toronto", "https://example.com/files/mixtape/A.mp3")[0]
    assert not app_module._download_allowed(
        "toronto", "https://torontoravemixtapearchive.com/track/1")[0]
    assert app_module.DOWNLOAD_SOURCE_LABELS["toronto"] == "Toronto Rave Mixtape Archive"
    assert app_module.DOWNLOAD_SOURCE_ART["toronto"].endswith("toronto.png")


def test_toronto_sticker_is_a_512px_png():
    path = os.path.join(os.path.dirname(HERE), "static", "art", "toronto.png")
    with open(path, "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"
        handle.read(4)
        assert handle.read(4) == b"IHDR"
        width, height = struct.unpack(">II", handle.read(8))
    assert (width, height) == (512, 512)


def test_toronto_sticker_is_served(web):
    response = web.get("/static/art/toronto.png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
