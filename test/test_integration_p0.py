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


def test_rtp_hub_timeout_retries_once_without_a_negative_mark(web, monkeypatch):
    """A timeout is transient: retry once, and do not lock the next request."""
    calls = []

    def timeout(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        return None

    monkeypatch.setattr(legacy.http, "fetch", timeout)
    monkeypatch.setattr(legacy.http, "last_failure", lambda: ("timeout", None))
    cache.clear_memory()

    first = web.get("/api/legacy/ravetapepacks")
    assert first.status_code == 502
    payload = first.get_json()
    assert payload["code"] == "source_unavailable"
    assert "(0)" not in str(payload)
    # One bounded retry inside the same request, then stop.
    assert len(calls) == 2
    # The first attempt gets the measured-p99 deadline (>= 25 s), not the old 6 s.
    assert calls[0] >= 25

    # A later request must try again: a timeout must never negative-cache.
    second = web.get("/api/legacy/ravetapepacks")
    assert second.status_code == 502
    assert len(calls) == 4


def test_rtp_hub_http_error_sets_a_short_negative_mark(web, monkeypatch):
    """A definitive HTTP error may be remembered, but child routes still fetch."""
    calls = []

    def blocked(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(legacy.http, "fetch", blocked)
    monkeypatch.setattr(legacy.http, "last_failure", lambda: ("http", 403))
    cache.clear_memory()

    first = web.get("/api/legacy/ravetapepacks")
    assert first.status_code == 502
    assert len(calls) == 1  # HTTP status is definitive; no second attempt

    # Inside the short window the hub does not hit the network again...
    second = web.get("/api/legacy/ravetapepacks")
    assert second.status_code == 502
    assert len(calls) == 1

    # ...but a child route owns its own page and is never gated by the hub.
    monkeypatch.setattr(legacy, "category_page",
                        lambda cat_id, page: ([{"id": "1", "name": "Pack"}], ""))
    assert web.get("/api/legacy/ravetapepacks/index/events/363").status_code == 200
    # RTP_FAILURE_TTL is short (15 s), not the old 60 s.
    assert legacy.RTP_FAILURE_TTL <= 20


def test_rtp_post_that_really_does_not_exist_stays_404(web, monkeypatch):
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(legacy.http, "last_failure", lambda: ("http", 404))
    cache.clear_memory()
    response = web.get("/api/legacy/ravetapepacks/post/999")
    assert response.status_code == 404


def test_rtp_post_during_hub_outage_is_502_not_404(web, monkeypatch):
    def unavailable(post_id):
        raise legacy.SourceUnavailable(post_id)

    monkeypatch.setattr(legacy, "post", unavailable)
    response = web.get("/api/legacy/ravetapepacks/post/899")
    assert response.status_code == 502
    assert response.get_json()["code"] == "source_unavailable"


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
