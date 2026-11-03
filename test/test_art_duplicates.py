# -*- coding: utf-8 -*-
"""Duplicate-art guard for the web build (1.6.40).

The owner's rule: distinct navigation rows must never show the same sticker.
Two rows may share a file only when they are the same destination (same label)
or when a leaf list reuses its parent category tile — those exceptions are
listed here explicitly, so a new duplicate has to be a deliberate decision.
"""

import os
import re

import struct

import pytest

import app as app_module

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ART = os.path.join(ROOT, "static", "art")

# Same concept, different surface labels: the rail shortens the section name.
LABEL_ALIASES = {
    frozenset(("SoundCloud Sets", "SoundCloud Picks")),
}

# Leaf/dynamic lists intentionally reuse their category tile.
SHARED_LEAF_LISTS = {
    "/api/years",       # every year row is the same "By Year" category
    "/api/raves",       # every rave night is the same "Rave Nights" folder
    "/api/pirate",      # every station is the same pirate-archive folder
    "/api/podcasts",    # every feed is the same podcasts folder
    "/api/legacy/jungletapes",  # every show is the same JungleTapes folder
}


def _items(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    return response.get_json()["items"]


def _png_size(path):
    with open(path, "rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("not a PNG: %s" % path)
        handle.read(4)
        if handle.read(4) != b"IHDR":
            raise ValueError("missing IHDR: %s" % path)
        return struct.unpack(">II", handle.read(8))


def _icon_name(icon):
    return os.path.basename(str(icon or ""))


def _check_unique(rows, source):
    """rows = [(label, icon-path)]; identical labels may share a tile."""
    seen = {}
    for label, icon in rows:
        seen.setdefault(_icon_name(icon), set()).add(label)
    for icon, labels in seen.items():
        if len(labels) > 1:
            assert frozenset(labels) in LABEL_ALIASES, (
                "%s: distinct rows share %s -> %r" % (source, icon, sorted(labels)))


def test_section_and_rail_icons_are_distinct(web):
    menu = web.get("/api/menu").get_json()
    sections = [(row["label"], row["icon"]) for row in menu["sections"]]
    rail = [(row["label"], row["icon"]) for row in menu["rail"]]
    _check_unique(sections, "/api/menu sections")
    _check_unique(rail, "/api/menu rail")
    _check_unique(sections + rail, "/api/menu sections+rail")
    assert len(sections) == 23
    assert len(rail) == 9


def test_pack_genre_rows_are_distinct(web):
    rows = [(item["label"], item["art"]) for item in _items(web, "/api/packs")]
    assert len(rows) == 7
    _check_unique(rows, "/api/packs")
    expected = {row[1]: app_module.PACK_ART[row[0]] for row in app_module.ARCHIVE_SECTIONS}
    for label, icon in rows:
        assert _icon_name(icon) == expected[label]
    _check_unique(rows + [(row["label"], row["icon"]) for row in
                          web.get("/api/menu").get_json()["sections"]],
                  "/api/packs + sections")


def test_live_genre_rows_are_distinct(web):
    rows = [(item["label"], item["art"]) for item in _items(web, "/api/live")]
    assert len(rows) == 7
    _check_unique(rows, "/api/live")
    assert {_icon_name(item["art"]) for item in _items(web, "/api/live")} == \
        set(app_module.LIVE_ART.values())


def test_filter_rows_are_distinct(web):
    rows = [(item["label"], item["art"]) for item in _items(web, "/api/filters")]
    assert len(rows) == len(app_module._filters())
    _check_unique(rows, "/api/filters")
    assert {_icon_name(item["art"]) for item in _items(web, "/api/filters")} == \
        set(app_module.FILTER_ART.values())


def test_legacy_rows_are_distinct(web):
    rows = [(item["label"], item["art"]) for item in _items(web, "/api/legacy")]
    _check_unique(rows, "/api/legacy")
    assert [label for label, _icon in rows] == [
        "RaveTapePacks.com", "JungleTapes.com"]


def test_no_leaf_list_shares_icons_between_rows(web):
    for path in SHARED_LEAF_LISTS:
        rows = [(item["label"], item["art"]) for item in _items(web, path)]
        if not rows:
            continue
        icons = {_icon_name(icon) for _label, icon in rows}
        assert len(icons) == 1, "%s leaf rows should share one category tile" % path


def test_no_row_uses_the_generic_rave_tile_unless_it_is_latest(web):
    menu = web.get("/api/menu").get_json()
    for row in menu["sections"] + menu["rail"]:
        if row["label"] == "Latest":
            assert _icon_name(row["icon"]) == "rave.png"
        else:
            assert _icon_name(row["icon"]) != "rave.png", row["label"]
    for path in ("/api/packs", "/api/live", "/api/filters", "/api/legacy"):
        for item in _items(web, path):
            assert _icon_name(item["art"]) != "rave.png", (path, item["label"])


def test_every_referenced_png_exists_and_is_lowercase():
    names = set(re.findall(r'"([a-z0-9-]+\.png)"', open(
        os.path.join(ROOT, "app.py"), encoding="utf-8").read()))
    js = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    names.update(re.findall(r'/static/art/([a-z0-9-]+\.png)', js))
    missing = sorted(name for name in names if not os.path.exists(os.path.join(ART, name)))
    assert missing == [], "missing art files: %r" % missing
    for name in names:
        assert name == name.lower(), name


def test_web_icons_are_square_and_sized_for_the_rail():
    for name in sorted(os.listdir(ART)):
        if not name.endswith(".png") or name.startswith("icon"):
            continue
        width, height = _png_size(os.path.join(ART, name))
        assert width == height, name
        assert width in (256, 512), (name, width)


def test_art_tables_cover_every_menu_row():
    section_icons = {row[2] for row in app_module.SECTIONS}
    assert section_icons & {row["icon"] for row in app_module.RAIL} == {
        "rave.png", "tapepacks.png", "radio.png", "favourites.png",
        "downloads.png", "soundcloud.png"}
    assert set(app_module.PACK_ART) == {row[0] for row in app_module.ARCHIVE_SECTIONS}
    assert set(app_module.LIVE_ART) == {row[0] for row in app_module.RADIO_SECTIONS}
    assert set(app_module.FILTER_ART) == {
        entry["id"] for entry in app_module._filters()}
