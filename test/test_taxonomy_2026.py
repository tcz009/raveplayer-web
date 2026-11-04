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
    payload = web.get("/api/legacy/ravetapepacks").get_json()
    assert _labels(payload) == [
        "By Event (50)", "By DJ (209)", "By MC (90)", "By Year", "All Packs"]
    assert payload["items"][1]["open"] == "/api/legacy/ravetapepacks/index/djs"
    dj = web.get("/api/legacy/ravetapepacks/index/djs").get_json()
    assert len(dj["items"]) == 209
    mc = web.get("/api/legacy/ravetapepacks/index/mcs").get_json()
    assert len(mc["items"]) == 90


def test_ravetapepacks_category_page_lists_its_packs(monkeypatch):
    monkeypatch.setattr(legacy, "category", lambda kind, cat_id: {"id": cat_id, "name": "Brockie"})
    monkeypatch.setattr(legacy, "categories", lambda: {
        "events": [], "djs": [{"id": "106", "name": "Brockie"}], "mcs": [], "years": []})
    monkeypatch.setattr(legacy.http, "fetch", lambda url, timeout=None, ttl=0, headers=None:
                        _fixture("ravetapepacks-category.html"))
    packs = legacy.category_packs("djs", "106")
    assert packs and all(pack["tracks"] for pack in packs)


def test_no_legacy_bucket_or_legacy_toronto_route(web):
    assert web.get("/api/legacy").status_code == 404
    assert web.get("/api/legacy/toronto").status_code == 404
    assert "legacy" not in [row[0] for row in app_module.SECTIONS]
