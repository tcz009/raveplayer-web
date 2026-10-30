# -*- coding: utf-8 -*-
"""UK garage hub and Mixcloud lookup — offline tests with stubbed providers."""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app  # noqa: E402
from core import mixcloud  # noqa: E402


@pytest.fixture()
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", str(tmp_path))
    app.app.config.update(TESTING=True)
    return app.app.test_client()


def test_embed_url_encodes_the_page():
    url = mixcloud.embed_url("https://www.mixcloud.com/djez/nuvolve-046/")
    assert url.startswith("https://www.mixcloud.com/widget/iframe/?")
    assert "feed=https%3A%2F%2Fwww.mixcloud.com%2Fdjez%2Fnuvolve-046%2F" in url


def test_search_skips_unexpected_entries(monkeypatch):
    payload = {
        "data": [
            {"name": "DJ EZ NUVOLVE 046", "url": "https://www.mixcloud.com/djez/nuvolve-046/",
             "user": {"name": "DJ EZ"}, "pictures": {"medium": "http://img/1.jpg"}, "created_time": "2026-01-01"},
            {"name": "bad", "url": "http://example.com/not-mixcloud"},
            "not a dict",
        ]
    }
    monkeypatch.setattr(mixcloud.fetch, "fetch_json", lambda *a, **k: payload)
    results = mixcloud.search("DJ EZ")
    assert len(results) == 1
    assert results[0]["title"] == "DJ EZ NUVOLVE 046"
    assert results[0]["user"] == "DJ EZ"
    assert results[0]["embed"].startswith("https://www.mixcloud.com/widget/iframe/")


def test_search_handles_bad_payload(monkeypatch):
    monkeypatch.setattr(mixcloud.fetch, "fetch_json", lambda *a, **k: None)
    assert mixcloud.search("x") == []


def test_ukg_hub_merges_mixcloud_and_archive(web, monkeypatch):
    monkeypatch.setattr(mixcloud, "search", lambda query, limit=24: [
        {"title": "DJ EZ presents NUVOLVE radio 046", "url": "https://www.mixcloud.com/djez/nuvolve-046/",
         "user": "DJ EZ", "embed": "https://www.mixcloud.com/widget/iframe/?feed=x", "thumb": "", "created": "2026-01-01"},
    ])
    monkeypatch.setattr(app.archive, "search", lambda *a, **k: (
        [{"identifier": "garage-nation-pack", "title": "Garage Nation Pack", "year": "1999"}], 1))
    response = web.get("/api/ukg")
    assert response.status_code == 200
    items = response.get_json()["items"]
    embedded = [item for item in items if "embed" in item]
    folders = [item for item in items if "open" in item]
    assert len(embedded) == 1
    assert embedded[0]["embed"].startswith("https://www.mixcloud.com/widget/")
    assert folders and folders[0]["open"].startswith("/api/item/")


def test_mixcloud_endpoint_shape(web, monkeypatch):
    monkeypatch.setattr(app.mixcloud, "search", lambda query, limit=24: [
        {"title": "Set", "url": "https://www.mixcloud.com/a/b/", "user": "U",
         "embed": "https://www.mixcloud.com/widget/iframe/?feed=y", "thumb": "", "created": "2026-02-02"},
    ])
    payload = web.get("/api/mixcloud?q=DJ%20EZ").get_json()
    assert payload["title"].startswith("Mixcloud:")
    assert payload["items"][0]["label"] == "Set"
    assert "embed" in payload["items"][0]


def test_mixcloud_endpoint_searches_once_and_pairs_its_own_entries(web, monkeypatch):
    """Regression: the route used to search twice and zip the two lists.

    A cache expiry between the calls could pair a title with a different
    set's embed URL (audit risk 3). The route must call search exactly once
    and copy embed/external from that same list.
    """
    calls = []
    first = [
        {"title": "First set", "url": "https://www.mixcloud.com/a/one/", "user": "A",
         "embed": "https://www.mixcloud.com/widget/iframe/?feed=one", "thumb": "",
         "created": "2026-01-01"},
        {"title": "Second set", "url": "https://www.mixcloud.com/b/two/", "user": "B",
         "embed": "https://www.mixcloud.com/widget/iframe/?feed=two", "thumb": "",
         "created": ""},
    ]
    second = [
        {"title": "WRONG", "url": "https://www.mixcloud.com/wrong/", "user": "X",
         "embed": "https://www.mixcloud.com/widget/iframe/?feed=wrong", "thumb": "",
         "created": ""},
    ]

    def fake_search(query, limit=24):
        calls.append((query, limit))
        return first if len(calls) == 1 else second

    monkeypatch.setattr(app.mixcloud, "search", fake_search)
    payload = web.get("/api/mixcloud?q=careful&limit=2").get_json()
    assert calls == [("careful", 2)], "search must be called exactly once"
    items = payload["items"]
    assert [item["label"] for item in items] == ["First set", "Second set"]
    assert items[0]["embed"] == "https://www.mixcloud.com/widget/iframe/?feed=one"
    assert items[0]["external"] == "https://www.mixcloud.com/a/one/"
    assert items[1]["embed"] == "https://www.mixcloud.com/widget/iframe/?feed=two"
    assert items[1]["external"] == "https://www.mixcloud.com/b/two/"
    assert "WRONG" not in [item["label"] for item in items]


def test_garage_filters_exist():
    import json
    path = os.path.join(os.path.dirname(HERE), "filters.json")
    names = {entry["name"] for entry in json.load(open(path, encoding="utf-8"))}
    assert "DJ EZ" in names
    assert "Garage Nation" in names
    assert "One In The Jungle" in names


def test_uk_garage_archive_section_registered():
    keys = {entry[0] for entry in app.ARCHIVE_SECTIONS}
    assert "garage" in keys
    radio_keys = {entry[0] for entry in app.RADIO_SECTIONS}
    assert "ukg" in radio_keys


def test_soundcloud_registry_and_embed():
    from core import soundcloud
    entries = soundcloud.registry()
    assert entries, "curated SoundCloud sets should not be empty"
    assert all(entry["url"].startswith("https://soundcloud.com/") for entry in entries)
    embed = soundcloud.embed_url(entries[0]["url"])
    assert embed.startswith("https://w.soundcloud.com/player/?url=https%3A%2F%2Fsoundcloud.com%2F")


def test_soundcloud_endpoint_shape(web):
    payload = web.get("/api/soundcloud").get_json()
    assert payload["title"] == "SoundCloud Sets"
    assert payload["items"]
    assert payload["items"][0]["embed"].startswith("https://w.soundcloud.com/player/")
    assert payload["items"][0]["external"].startswith("https://soundcloud.com/")


def test_soundcloud_search_requires_client_id(monkeypatch):
    from core import soundcloud
    monkeypatch.setattr(soundcloud, "CLIENT_ID", "")
    assert soundcloud.configured() is False
    assert soundcloud.search("jungle") == []


def test_soundcloud_search_parses_tracks(monkeypatch):
    from core import soundcloud
    monkeypatch.setattr(soundcloud, "CLIENT_ID", "fake")
    payload = [{"title": "Live at Kool FM", "permalink_url": "https://soundcloud.com/a/b",
                "user": {"username": "A"}, "artwork_url": "", "duration": 100}]
    monkeypatch.setattr(soundcloud.fetch, "fetch_json", lambda *a, **k: payload)
    results = soundcloud.search("kool")
    assert results[0]["title"] == "Live at Kool FM"
    assert results[0]["embed"].startswith("https://w.soundcloud.com/player/")


def _app_js():
    path = os.path.join(os.path.dirname(HERE), "static", "app.js")
    return open(path, encoding="utf-8").read()


def test_search_submit_opens_a_pasted_set_before_archive_search():
    """Regression: the 1.4.2 claim was dead code — the submit handler always
    navigated to /api/search and externalEmbed() was never called (audit
    risk 2).  Assert the actual handler, not just that the function exists."""
    source = _app_js()
    start = source.index('$("search-form").addEventListener("submit"')
    end = source.index('$("back").addEventListener', start)
    handler = source[start:end]
    assert "const embed = externalEmbed(query);" in handler
    assert "if (embed)" in handler
    assert "openEmbed(embed)" in handler
    # The embed branch must run before the normal Archive navigation.
    assert handler.index("externalEmbed(query)") < handler.index("navigate(`/api/search")
    assert handler.index("openEmbed(embed)") < handler.index("navigate(`/api/search")
    # Normal text search is unchanged.
    assert "navigate(`/api/search?q=${encodeURIComponent(query)}`)" in handler
    assert 'toast("Type something to search")' in handler


def test_client_embed_iframes_are_allowed_by_the_csp():
    """The widget hosts hard-coded in app.js must be the hosts the CSP frames.

    A change to a widget URL without the matching `frame-src` entry silently
    breaks the embed panel in a browser but passes every endpoint test."""
    from urllib.parse import urlsplit
    source = _app_js()
    for widget in ("https://w.soundcloud.com/player/",
                   "https://www.mixcloud.com/widget/iframe/"):
        assert widget in source, "app.js no longer uses %s" % widget
        assert "https://" + urlsplit(widget).netloc in app.CSP, \
            "%s is not allowed by the CSP frame-src" % widget
    # Mixcloud's widget redirects to player-widget.mixcloud.com; CSP checks
    # every redirect hop, so the canonical widget host alone is not enough.
    assert "https://player-widget.mixcloud.com" in app.CSP


def test_every_server_section_has_a_client_route():
    """Regression: 'ukg' was missing from the JS map, so UK Garage opened
    Latest and both showed as active."""
    import re
    source = open(os.path.join(os.path.dirname(HERE), "static", "app.js"), encoding="utf-8").read()
    block = re.search(r"const map = \{(.*?)\};", source, re.S)
    assert block, "section map not found"
    mapped = set(re.findall(r"([a-z-]+):\s*\"/api/", block.group(1)))
    for section_id, *_ in app.SECTIONS:
        assert section_id in mapped, "no client route for section %r" % section_id


def test_rave_catalog_loads_and_has_content():
    from core import raves
    entries = raves.registry()
    assert len(entries) >= 40, "research catalogue should be loaded"
    assert any(e["archive_count"] for e in entries)
    assert any(e["mixcloud_count"] for e in entries)
    assert all(e["slug"] for e in entries)


def test_raves_section_and_detail(web, monkeypatch):
    from core import raves
    monkeypatch.setattr(raves, "registry", lambda: [
        {"name": "Pure X", "slug": "pure-x", "type": "promotion",
         "archive_query": 'title:("Pure X") AND mediatype:audio', "mixcloud_query": "pure x rave",
         "archive_count": 2, "mixcloud_count": 3},
    ])
    monkeypatch.setattr(app.mixcloud, "search", lambda *a, **k: [
        {"title": "Pure X set", "url": "https://www.mixcloud.com/a/b/", "user": "U",
         "embed": "https://www.mixcloud.com/widget/iframe/?feed=z", "thumb": "", "created": ""},
    ])
    monkeypatch.setattr(app.archive, "search", lambda *a, **k: (
        [{"identifier": "pure-x-pack", "title": "Pure X 1993", "year": "1993"}], 2))
    items = web.get("/api/raves").get_json()["items"]
    assert items[0]["label"] == "Pure X"
    detail = web.get("/api/rave/pure-x").get_json()
    assert detail["title"] == "Pure X"
    assert any("embed" in item for item in detail["items"])
    assert any("open" in item for item in detail["items"])


def test_broken_filters_now_use_archive_wide_queries():
    import json
    path = os.path.join(os.path.dirname(HERE), "filters.json")
    entries = {entry["id"]: entry for entry in json.load(open(path, encoding="utf-8"))}
    assert "collection:ravetapes" not in entries["centreforce"]["query"]
    assert "collection:ravetapes" not in entries["don-fm"]["query"]
    assert "collection:ravetapes" not in entries["slammin-vinyl"]["query"]
