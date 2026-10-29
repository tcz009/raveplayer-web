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


def test_garage_filters_exist():
    import json
    path = os.path.join(os.path.dirname(HERE), "filters.json")
    names = {entry["name"] for entry in json.load(open(path, encoding="utf-8"))}
    assert "DJ EZ" in names
    assert "Garage Nation" in names


def test_uk_garage_archive_section_registered():
    keys = {entry[0] for entry in app.ARCHIVE_SECTIONS}
    assert "garage" in keys
    radio_keys = {entry[0] for entry in app.RADIO_SECTIONS}
    assert "ukg" in radio_keys
