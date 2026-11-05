# -*- coding: utf-8 -*-
"""1.6.44 scene taxonomy: exact labels/order, source placement and indexes."""

import os
import re

import pytest

import app as app_module
from core import legacy

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def _labels(payload):
    return [item["label"] for item in payload["items"]]


def _walk(web, url):
    """Follow the pager's next links and return every item exactly once."""
    items, page = [], 1
    while True:
        separator = "&" if "?" in url else "?"
        payload = web.get("%s%spage=%d" % (url, separator, page)).get_json()
        items.extend(payload["items"])
        if not (payload.get("pager") or {}).get("next"):
            return items
        page += 1


def _opens(payload):
    return [item.get("open") for item in payload["items"]]


def test_web_rail_is_the_exact_taxonomy_order(web):
    menu = web.get("/api/menu").get_json()
    assert [group["id"] for group in menu["rail"]] == [
        "latest", "packs", "pirate", "shows", "live", "favourites", "downloads"]
    assert [group["label"] for group in menu["rail"]] == [
        "Latest", "Tape Packs", "Pirate Radio", "Mixes & Shows", "Live Radio",
        "Favourites", "Downloads"]
    assert len(menu["rail"]) == 7


def test_tape_packs_rows_one_to_seven_are_exact(web):
    payload = web.get("/api/packs").get_json()
    assert payload["title"] == "Tape Packs"
    assert _labels(payload) == [
        "Toronto Rave Mixtape Archive", "JungleTapes.com", "RaveTapePacks.com",
        "Classic Pack Series", "By Rave, Venue & Promoter", "By Year", "Every Pack"]
    assert _opens(payload) == [
        "/api/toronto", "/api/legacy/jungletapes", "/api/legacy/ravetapepacks",
        "/api/filters", "/api/raves", "/api/years", "/api/every-pack"]
    rail = web.get("/api/rail/packs").get_json()
    assert ([item["label"] for item in rail["items"]] ==
            [item["label"] for item in payload["items"]])


def test_pirate_and_shows_hubs_are_exact(web):
    pirate = web.get("/api/rail/pirate").get_json()
    assert _labels(pirate) == ["Hardscore", "Pirate Radio Archives", "Kool FM Sets"]
    assert _opens(pirate) == ["/api/hardscore", "/api/pirate", "/api/filter/kool"]
    shows = web.get("/api/rail/shows").get_json()
    assert _labels(shows) == ["Podcasts & Mixes", "DNBRADIO", "GetDarker", "Moxser"]


def test_genre_doors_are_split_and_old_labels_are_gone(web):
    ids = [row[0] for row in app_module.ARCHIVE_SECTIONS]
    labels = [row[1] for row in app_module.ARCHIVE_SECTIONS]
    assert "jungle" in ids and "dnb" in ids and "hardcore" in ids and "happy-hardcore" in ids
    assert "Jungle / Drum & Bass" not in labels
    assert "Oldskool & Rave" not in [row[1] for row in app_module.RADIO_SECTIONS]
    assert "Breakbeat" not in [row[1] for row in app_module.RADIO_SECTIONS]
    assert "Jungle" in [row[1] for row in app_module.RADIO_SECTIONS]
    assert "Drum & Bass" in [row[1] for row in app_module.RADIO_SECTIONS]
    assert "Happy Hardcore" in labels


def test_live_radio_stations_then_genres(web):
    payload = web.get("/api/live").get_json()
    labels = _labels(payload)
    assert labels[:3] == ["Kool FM", "Rinse FM", "Centreforce 883"]
    assert labels[3:] == ["Hardcore", "Jungle", "Drum & Bass", "House",
                          "UK Garage", "Dubstep & Grime"]
    # Stations are playable rows, genres are doors.
    assert all(item.get("play") for item in payload["items"][:3])
    assert all(item.get("open") for item in payload["items"][3:])


def test_section_ids_are_kebab_case_and_paths_are_consistent():
    for section_id, _label, _art, _sub in app_module.SECTIONS:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", section_id), section_id
        assert app_module.SECTION_PATHS[section_id].startswith("/api/")
    for section_id, path in app_module.SECTION_PATHS.items():
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", section_id)
        assert path.startswith("/api/")


def test_ravetapepacks_nav_indexes_have_the_published_counts():
    categories = legacy.parse_categories(_fixture("ravetapepacks-nav.html"))
    assert len(categories["events"]) == 50
    assert len(categories["djs"]) == 209
    assert len(categories["mcs"]) == 90
    assert len(categories["years"]) == 13
    assert categories["djs"][0]["name"] == "Adam F"
    assert categories["mcs"][0]["name"] == "5ive-O"


def test_ravetapepacks_index_routes_expose_by_dj_and_by_mc(web, monkeypatch):
    cats = legacy.parse_categories(_fixture("ravetapepacks-nav.html"))
    monkeypatch.setattr(app_module.legacy, "categories", lambda: cats)
    monkeypatch.setattr(app_module.legacy, "categories_state", lambda: cats)
    payload = web.get("/api/legacy/ravetapepacks").get_json()
    assert _labels(payload) == [
        "By Event (50)", "By DJ (209)", "By MC (90)", "By Year (13)", "All Packs"]
    assert payload["items"][1]["open"] == "/api/legacy/ravetapepacks/index/djs"
    dj = _walk(web, "/api/legacy/ravetapepacks/index/djs")
    assert len(dj) == 209
    mc = _walk(web, "/api/legacy/ravetapepacks/index/mcs")
    assert len(mc) == 90


def test_ravetapepacks_full_homepage_ignores_other_categories():
    categories = legacy.parse_categories(_fixture("bench/rtp-home.html"))
    assert {kind: len(categories[kind]) for kind in legacy.CATEGORY_KINDS} == {
        "events": 50, "djs": 209, "mcs": 90, "years": 13}
    included = set()
    for kind in legacy.CATEGORY_KINDS:
        included.update(entry["id"] for entry in categories[kind])
    assert "13" not in included  # "The Edge" and other sidebar-only rows


def test_ravetapepacks_category_pages_are_paginated(monkeypatch):
    pages = {
        legacy.BASE + "?cat=5": _fixture("ravetapepacks-category-paged.html"),
        legacy.BASE + "?cat=5&paged=2": _fixture("ravetapepacks-category-paged-2.html"),
    }
    urls = []

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        urls.append(url)
        return pages.get(url)

    monkeypatch.setattr(legacy.http, "fetch", fake_fetch)
    posts = legacy.category("5")

    assert urls == [legacy.BASE + "?cat=5", legacy.BASE + "?cat=5&paged=2"]
    assert len(posts) == 10
    assert posts[0] == {"id": "899", "name": "Dreamscape 7"}
    assert posts[-1] == {"id": "37", "name": "Dreamscape 2"}


def test_ravetapepacks_post_page_tracks(monkeypatch):
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k:
                        _fixture("ravetapepacks-post.html"))
    entry = legacy.post("899")

    assert entry["name"] == "Dreamscape 7"
    assert len(entry["tracks"]) == 15
    assert entry["tracks"][0]["name"] == "Charlie Hall"
    assert entry["tracks"][0]["url"].endswith(
        "/music/Dreamscape7/Charlie_Hall.mp3")


def test_ravetapepacks_category_route_lists_post_folders(web, monkeypatch):
    # The title comes from the cached nav (perf/overhaul: a category view must
    # not add a homepage fetch), so that is the helper the route uses.
    monkeypatch.setattr(legacy, "cached_categories", lambda: {
        "events": [], "djs": [{"id": "106", "name": "Brockie"}],
        "mcs": [], "years": []})
    monkeypatch.setattr(legacy, "category_page", lambda cat_id, page=1: ([
        {"id": "899", "name": "Dreamscape 7"}] if cat_id == "106" else [], ""))
    payload = web.get("/api/legacy/ravetapepacks/index/djs/106").get_json()
    assert payload["title"] == "Brockie"
    assert _labels(payload) == ["Dreamscape 7"]
    assert payload["items"][0]["open"] == "/api/legacy/ravetapepacks/post/899"


def test_ravetapepacks_post_route_lists_tracks(web, monkeypatch):
    monkeypatch.setattr(legacy, "post", lambda post_id: {
        "key": post_id, "name": "Dreamscape 7",
        "tracks": [{"name": "Charlie Hall",
                    "url": "https://www.ravetapepacks.com/music/"
                           "Dreamscape7/Charlie_Hall.mp3"}],
    } if post_id == "899" else None)
    payload = web.get("/api/legacy/ravetapepacks/post/899").get_json()
    assert payload["title"] == "Dreamscape 7"
    assert _labels(payload) == ["Charlie Hall"]
    assert payload["items"][0]["play"].endswith(legacy.REFERER_SUFFIX)


def test_ravetapepacks_post_route_rejects_bad_ids(web, monkeypatch):
    called = []
    monkeypatch.setattr(legacy, "post", lambda post_id: called.append(post_id))
    assert web.get("/api/legacy/ravetapepacks/post/abc").status_code == 404
    assert web.get("/api/legacy/ravetapepacks/post/0000000000000").status_code == 404
    assert called == []


def test_no_legacy_bucket_or_legacy_toronto_route(web):
    assert web.get("/api/legacy").status_code == 404
    assert web.get("/api/legacy/toronto").status_code == 404
    assert "legacy" not in [row[0] for row in app_module.SECTIONS]


def test_ravetapepacks_index_is_complete_by_default_and_sliceable(web, monkeypatch):
    cats = {"events": [], "djs": [], "mcs": [], "years": []}
    cats["djs"] = [{"id": str(100 + index), "name": "DJ %02d" % index}
                   for index in range(12)]
    monkeypatch.setattr(legacy, "categories", lambda: cats)

    full = web.get("/api/legacy/ravetapepacks/index/djs").get_json()
    assert len(full["items"]) == 12
    assert "next" not in full

    first = web.get("/api/legacy/ravetapepacks/index/djs?limit=5").get_json()
    assert len(first["items"]) == 5
    assert first["next"].endswith("limit=5&offset=5")

    second = web.get("/api/legacy/ravetapepacks/index/djs?limit=5&offset=5").get_json()
    assert len(second["items"]) == 5
    assert second["next"].endswith("limit=5&offset=10")

    last = web.get("/api/legacy/ravetapepacks/index/djs?limit=5&offset=10").get_json()
    assert len(last["items"]) == 2
    assert "next" not in last
    assert (first["items"] + second["items"] + last["items"]) == full["items"]


def test_ravetapepacks_all_is_paginated_and_complete(web, monkeypatch):
    posts = [{"id": str(2450 + index), "name": "Pack %02d" % index}
             for index in range(8)]

    def fake_page(page=1):
        start = (page - 1) * 3
        window = posts[start:start + 3]
        return window, ("more" if start + 3 < len(posts) else "")

    monkeypatch.setattr(legacy, "all_packs", fake_page)

    first = web.get("/api/legacy/ravetapepacks/all").get_json()
    assert len(first["items"]) == 3
    assert first["next"].endswith("page=2")
    assert first["items"][0]["open"] == "/api/legacy/ravetapepacks/post/2450"

    second = web.get(first["next"]).get_json()
    third = web.get(second["next"]).get_json()
    assert "next" not in third
    labels = [item["label"] for page in (first, second, third)
              for item in page["items"]]
    assert labels == ["Pack %02d" % index for index in range(8)]


def test_ravetapepacks_all_packs_reads_the_blog_archive(monkeypatch):
    page = _fixture("ravetapepacks-all-packs.html")
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)
    posts, nxt = legacy.all_packs(1)
    assert [post["id"] for post in posts] == ["2687", "2669", "2666"]
    assert posts[0]["name"] == "The Best Of British – The Jubilee Jam"
    assert nxt == legacy.BASE + "?cat=0&paged=2"


def test_ravetapepacks_category_route_pages_with_next(web, monkeypatch):
    monkeypatch.setattr(legacy, "categories", lambda: {
        "events": [], "djs": [{"id": "106", "name": "Brockie"}],
        "mcs": [], "years": []})
    calls = []

    def fake_page(cat_id, page=1):
        calls.append((cat_id, page))
        if page == 1:
            return ([{"id": "899", "name": "Dreamscape 7"},
                     {"id": "879", "name": "Dreamscape 6"}], "more")
        return ([{"id": "37", "name": "Dreamscape 2"}], "")

    monkeypatch.setattr(legacy, "category_page", fake_page)
    first = web.get("/api/legacy/ravetapepacks/index/djs/106").get_json()
    assert calls == [("106", 1)]
    assert _labels(first) == ["Dreamscape 7", "Dreamscape 6"]
    assert first["next"].endswith("/index/djs/106?page=2")

    second = web.get(first["next"]).get_json()
    assert calls[-1] == ("106", 2)
    assert _labels(second) == ["Dreamscape 2"]
    assert "next" not in second


def test_jungletapes_shows_are_paginated(web, monkeypatch):
    shows = [{"key": "show-%03d" % index, "name": "Show %03d" % index}
             for index in range(120)]
    monkeypatch.setattr(legacy, "jungle_shows", lambda: shows)
    first = web.get("/api/legacy/jungletapes").get_json()
    assert len(first["items"]) == 40
    assert first["next"].endswith("page=2")
    last = web.get("/api/legacy/jungletapes?page=3").get_json()
    assert len(last["items"]) == 40
    assert "next" not in last
    assert len(_walk(web, "/api/legacy/jungletapes")) == 120


def test_jungletapes_show_dedupes_stream_and_download_fields(monkeypatch):
    page = _fixture("jungletapes-show.html")
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)
    tracks = legacy.jungle_tracks("assignment-earth_chry_1055_2000-10-23-000000")
    assert len(tracks) == 2
    assert [track["name"].rsplit(" .", 1)[-1] for track in tracks] == ["A", "B"]
    assert all("jungle_tapes_uploaded_audio_files" in track["url"] for track in tracks)


def test_ravetapepacks_track_exposes_a_download(web, monkeypatch):
    track_url = ("https://www.ravetapepacks.com/music/"
                 "Dreamscape7/Charlie_Hall.mp3")
    monkeypatch.setattr(legacy, "post", lambda post_id: {
        "key": post_id, "name": "Dreamscape 7",
        "tracks": [{"name": "Charlie Hall", "url": track_url}],
    } if post_id == "899" else None)
    payload = web.get("/api/legacy/ravetapepacks/post/899").get_json()
    item = payload["items"][0]
    assert item["download"] == {"source": "ravetapepacks"}
    assert item["play"].startswith(track_url)


def test_rtp_download_accepts_the_referer_suffix(web):
    import app as app_module
    plain = "https://www.ravetapepacks.com/music/Dreamscape7/Charlie_Hall.mp3"
    assert app_module._download_allowed("ravetapepacks", plain)[0]
    assert app_module._download_allowed("ravetapepacks", legacy.playable(plain))[0]
    assert not app_module._download_allowed(
        "ravetapepacks", "https://example.com/music/x.mp3")[0]
    assert not app_module._download_allowed(
        "ravetapepacks", "https://www.ravetapepacks.com/notes.txt")[0]


def test_favourites_round_trip_legacy_kinds(web):
    for entry in (
            {"kind": "legacy_post", "target": "899", "title": "Dreamscape 7"},
            {"kind": "legacy_jt", "target": "show-key", "title": "A show"},
            {"kind": "dnb", "target": "1234", "title": "A mix"}):
        assert web.post("/api/favourites", json=entry).get_json()["saved"] is True
    payload = web.get("/api/favourites").get_json()
    by_title = {item["label"]: item for item in payload["items"]}
    assert by_title["Dreamscape 7"]["open"].endswith("/post/899")
    assert by_title["A show"]["open"].endswith("/jungletapes/show-key")
    assert by_title["A mix"]["play_via"].endswith("/api/dnb/1234")
