# -*- coding: utf-8 -*-
"""1.6.15 navigation/chrome tests: the 8-button rail, pinned back button,
Surprise Me endpoint, and the brutalist theme tokens matching the Kodi pack.
"""

import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


RAIL_LABELS = [
    "Latest",
    "Tape Packs",
    "Mixes & Shows",
    "Club & Rave Sets",
    "SoundCloud Picks",
    "Live Radio",
    "Curated Gems",
    "Favourites",
    "Downloads",
]


def test_menu_rail_is_nine_groups_covering_every_section(web):
    import app as app_module

    payload = web.get("/api/menu").get_json()
    rail = payload["rail"]
    assert [group["label"] for group in rail] == RAIL_LABELS
    assert len(rail) == 9
    covered = []
    for group in rail:
        assert set(group) == {"id", "label", "icon", "sub", "path", "children"}
        assert group["path"].startswith("/api/")
        assert group["icon"].startswith("/static/art/")
        assert group["children"], group["id"]
        for child in group["children"]:
            assert set(child) == {"id", "path"}
            covered.append(child["id"])
    # Every nav section is reachable from exactly one rail group; search is
    # topbar-only by design.
    section_ids = {section[0] for section in app_module.SECTIONS}
    assert set(covered) == section_ids - {"search"}
    assert len(covered) == len(set(covered)), "a section is in two rail groups"


def test_rail_hub_returns_a_folder_per_section(web):
    payload = web.get("/api/rail/shows").get_json()
    assert payload["title"] == "Mixes & Shows"
    labels = [item["label"] for item in payload["items"]]
    assert labels == ["Podcasts & Mixes", "DNB Radio", "Moxser Mixes", "Hardscore"]
    assert [item["open"] for item in payload["items"]] == [
        "/api/podcasts", "/api/dnb", "/api/moxser", "/api/hardscore",
    ]

    packs = web.get("/api/rail/packs").get_json()
    assert [item["label"] for item in packs["items"]] == [
        "Tape Packs", "By Year", "Quick Filters",
    ]
    assert web.get("/api/rail/nope").status_code == 404


def test_section_paths_agree_between_server_and_client():
    import app as app_module

    source = _read("static/app.js")
    block = re.search(r"const map = \{(.*?)\};", source, re.S)
    assert block, "section map not found in app.js"
    client = dict(re.findall(r'([a-z-]+):\s*"(/api/[^"]+)"', block.group(1)))
    assert client == app_module.SECTION_PATHS


def test_back_button_is_a_large_sticky_control():
    html = _read("templates/index.html")
    assert 'id="back"' in html
    # It must live before the title row so it can stick while scrolling.
    assert html.index('id="back"') < html.index('class="stage-head"')
    assert "Back" in html[html.index('id="back"'):html.index('id="back"') + 120]

    css = _read("static/app.css")
    block = re.search(r"\.back \{(.*?)\n\}", css, re.S)
    assert block, ".back rule missing"
    body = block.group(1)
    assert "position: sticky" in body
    assert "min-height: 48px" in body
    assert "border: 2px solid var(--paper)" in body
    assert "top: calc(var(--topbar-h) + 10px)" in body
    assert "box-shadow: 4px 4px 0 var(--magenta)" in body
    # On the phone rail it clears the sticky 58px nav strip.
    assert ".back { top: calc(var(--topbar-h) + 58px + 10px); }" in css


def test_surprise_button_and_handler_are_wired():
    html = _read("templates/index.html")
    assert 'id="surprise"' in html and "Surprise me" in html
    js = _read("static/app.js")
    assert '/api/surprise' in js
    assert '$("surprise").addEventListener("click", surpriseMe)' in js
    assert "navigate(data.open)" in js
    # The SPA must never build markup from provider strings.
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_rail_is_built_from_the_grouped_payload():
    js = _read("static/app.js")
    assert "menu.rail" in js
    assert "state.rail = railSource" in js
    assert "railGroupActive" in js
    assert 'code.className = "rail-code"' in js and "RP-" in js
    assert 'code.setAttribute("aria-hidden", "true")' in js


def test_surprise_endpoint_opens_a_pack_or_show(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "_surprise_candidates", lambda: [{
        "label": "Boot Pack", "sub": "Random tape pack", "open": "/api/item/boot-pack",
    }])
    payload = web.get("/api/surprise").get_json()
    assert payload["open"] == "/api/item/boot-pack"
    assert payload["label"] == "Boot Pack"


def test_surprise_returns_503_when_nothing_is_available(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "_surprise_candidates", lambda: [])
    assert web.get("/api/surprise").status_code == 503


def test_surprise_candidates_cover_packs_and_shows(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.archive, "search",
                        lambda *a, **k: ([{"identifier": "pack-1", "title": "Pack One"}], 1))
    monkeypatch.setattr(app_module.feeds, "registry",
                        lambda: [{"id": "feed-1", "name": "Test Feed"}])
    candidates = app_module._surprise_candidates()
    opens = {candidate["open"] for candidate in candidates}
    assert "/api/item/pack-1" in opens
    assert "/api/podcast/feed-1" in opens


def test_theme_tokens_match_the_kodi_2026_pack():
    css = _read("static/app.css")
    tokens = {
        "--ink": "#07070b", "--paper": "#f2eee3", "--muted": "#a8a399",
        "--acid": "#e8ff00", "--magenta": "#ff2d8f", "--cyan": "#22e6ff",
        "--orange": "#ff6a1a",
    }
    for token, value in tokens.items():
        assert re.search(r"%s:\s*%s;" % (re.escape(token), value), css), token
    assert "Impact" in css
    # Print texture: halftone dots, hatch and the new scanline layer.
    halftone = re.search(r"\.halftone \{(.*?)\n\}", css, re.S).group(1)
    assert "repeating-linear-gradient(0deg" in halftone
    assert "radial-gradient" in halftone


def test_brand_and_character_are_untouched():
    """The identity guardrail: logo/mark/wordmark stay exactly as they were."""
    html = _read("templates/index.html")
    assert 'class="mark"' in html
    assert "<circle" in html and "<path" in html
    assert 'class="name"' in html and "RAVE<em>PLAYER</em>" in html
    assert 'class="tag"' in html and "ARCHIVE · 88 → NOW" in html
    css = _read("static/app.css")
    assert "box-shadow: 3px 3px 0 var(--magenta), inset 0 0 0 1px rgba(7, 7, 11, 0.55);" in css
