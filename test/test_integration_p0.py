# -*- coding: utf-8 -*-
"""P0 audit fixes on the web build (1.6.52).

Covers P0-3 (RaveTapePacks hub must fail fast and be negatively cached) and
P0-4 (an Archive pack detail is paginated).  P0-5's client policy is asserted
in ``test_source_blocked_ui.py``.
"""

import os

import app as app_module
from core import cache, legacy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def test_rtp_hub_timeout_is_a_fast_502_and_is_negatively_cached(web, monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(legacy.http, "fetch", timeout)
    cache.clear_memory()

    first = web.get("/api/legacy/ravetapepacks")
    assert first.status_code == 502
    payload = first.get_json()
    assert payload["code"] == "source_unavailable"
    assert "(0)" not in str(payload)

    # A retry inside the 60 s window must not hit the network again.
    second = web.get("/api/legacy/ravetapepacks")
    assert second.status_code == 502
    assert len(calls) == 1


def test_rtp_hub_success_uses_real_counts(web, monkeypatch):
    monkeypatch.setattr(legacy.http, "fetch",
                        lambda *args, **kwargs: _fixture("ravetapepacks-nav.html"))
    cache.clear_memory()
    response = web.get("/api/legacy/ravetapepacks")
    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == 5
    assert not any(label.endswith("(0)") for label in [item["label"] for item in items])


def test_rtp_category_does_not_touch_the_homepage(web, monkeypatch):
    calls = []
    monkeypatch.setattr(legacy.http, "fetch",
                        lambda *args, **kwargs: calls.append(args) or None)
    monkeypatch.setattr(legacy, "category_page",
                        lambda cat_id, page: ([{"id": "1", "name": "Pack"}],
                                              None))
    cache.clear_memory()
    response = web.get("/api/legacy/ravetapepacks/index/events/363")
    assert response.status_code == 200
    assert calls == []


def test_archive_pack_detail_is_paginated(web, monkeypatch):
    tracks = [{"title": "Tape %d" % index,
               "url": "https://archive.org/download/p/%d.mp3" % index,
               "length": "120"}
              for index in range(250)]
    monkeypatch.setattr(app_module.archive, "audio_files", lambda identifier: list(tracks))
    monkeypatch.setattr(app_module.archive, "thumb", lambda identifier: "https://archive.example/t.jpg")

    first = web.get("/api/item/helter-skelter-tape-packs")
    assert first.status_code == 200
    payload = first.get_json()
    assert len(payload["items"]) == 40
    assert payload["total"] == 250
    assert payload["next"].endswith("page=2")

    last = web.get("/api/item/helter-skelter-tape-packs?page=7")
    payload = last.get_json()
    assert len(payload["items"]) == 10
    assert "next" not in payload
