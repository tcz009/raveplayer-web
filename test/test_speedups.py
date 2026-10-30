# -*- coding: utf-8 -*-
"""Web-side speed and durability changes: radio TTL, parallel Latest,
cached podcast counts.

All offline: HTTP is monkeypatched down to ``core.fetch._request`` and the
on-disk cache gets a fresh directory per test.
"""

import datetime
import json
import threading

from core import feeds, latest, radio


def _rss(episodes):
    items = "".join(
        "<item><title>%s</title><enclosure url='%s' type='audio/mpeg'/></item>"
        % (title, url)
        for title, url in episodes
    )
    return "<?xml version='1.0'?><rss><channel><title>Feed</title>%s</channel></rss>" % items


# --------------------------------------------------------------------------- radio TTL


def test_web_radio_station_lists_use_the_ttl_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    requests = []
    payload = [{
        "name": "Jungle FM", "url_resolved": "http://radio.example:8000/live",
        "countrycode": "GB", "codec": "MP3", "bitrate": 128,
    }]

    def fake_request(url, timeout=20, headers=None):
        requests.append(url)
        return json.dumps(payload)

    monkeypatch.setattr(radio.http, "_request", fake_request)
    first = radio.stations("jungle", limit=10)
    second = radio.stations("jungle", limit=10)
    assert first == second == payload
    assert len(requests) == 1, "the second /api/live/<tag> must use the cache"
    assert radio.LIVE_TTL > 0
    # A different tag is a different directory query and must still fetch.
    radio.stations("garage", limit=10)
    assert len(requests) == 2


def test_web_radio_name_lookups_use_the_same_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    requests = []

    def fake_request(url, timeout=20, headers=None):
        requests.append(url)
        return json.dumps([{"name": "Kool FM", "url_resolved": "http://radio.example/kool"}])

    monkeypatch.setattr(radio.http, "_request", fake_request)
    assert radio.stations_by_name("Kool FM", limit=5)
    assert radio.stations_by_name("Kool FM", limit=5)
    assert len(requests) == 1


# --------------------------------------------------------------------------- latest merge


def test_merge_date_sorts_and_puts_undated_last(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(latest.feeds, "registry", lambda: [{"id": "f", "name": "Feed"}])
    monkeypatch.setattr(latest.feeds, "episodes", lambda feed_id, limit=6: [
        {"title": "Old episode", "url": "https://cdn/old.mp3",
         "published": "Mon, 01 Jan 2024 10:00:00 +0000"},
        {"title": "No date", "url": "https://cdn/none.mp3", "published": ""},
    ])
    monkeypatch.setattr(latest, "recent_packs", lambda rows=20: [{
        "kind": "pack", "title": "New pack", "identifier": "new-pack",
        "source": "Rave Tape Packs", "when": datetime.datetime(2026, 6, 1),
    }])
    monkeypatch.setattr(latest, "recent_dnb", lambda limit=12: [])

    merged = latest.merge(limit=10)
    assert [item["title"] for item in merged] == ["New pack", "Old episode", "No date"]
    assert latest.merge(limit=2) == merged[:2]


def test_merge_keeps_good_sources_when_one_provider_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(latest.feeds, "registry", lambda: [
        {"id": "good", "name": "Good Feed"},
        {"id": "bad", "name": "Bad Feed"},
    ])

    def fake_episodes(feed_id, limit=6):
        if feed_id == "bad":
            raise RuntimeError("feed on fire")
        return [{"title": "Good episode", "url": "https://cdn/good.mp3",
                 "published": "2026-01-01"}]

    def archive_boom(*args, **kwargs):
        raise RuntimeError("archive down")

    monkeypatch.setattr(latest.feeds, "episodes", fake_episodes)
    monkeypatch.setattr(latest, "recent_packs", archive_boom)
    monkeypatch.setattr(latest, "recent_dnb", lambda limit=12: [{
        "kind": "dnb", "title": "DNB show", "id": "1",
        "source": "DNB Radio", "when": None,
    }])

    merged = latest.merge(limit=10)
    assert [item["title"] for item in merged] == ["Good episode", "DNB show"]


def test_merge_fetches_independent_sources_in_parallel(tmp_path, monkeypatch):
    """A barrier only releases if the two providers run at the same time."""
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(latest.feeds, "registry", lambda: [])
    barrier = threading.Barrier(2, timeout=5)
    reached = []

    def provider(name):
        def run(*args, **kwargs):
            barrier.wait()
            reached.append(name)
            return []
        return run

    monkeypatch.setattr(latest, "recent_packs", provider("packs"))
    monkeypatch.setattr(latest, "recent_dnb", provider("dnb"))
    assert latest.merge(limit=10) == []
    assert not barrier.broken
    assert sorted(reached) == ["dnb", "packs"]


# --------------------------------------------------------------------------- feed counts


def _feed(monkeypatch):
    entry = {"id": "f", "name": "Feed", "url": "https://feeds.example/rss"}
    monkeypatch.setattr(feeds, "feed", lambda feed_id: entry if feed_id == "f" else None)
    return entry


def test_feed_count_reuses_the_body_cache_and_caches_the_count(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    _feed(monkeypatch)
    body = _rss([("One", "https://cdn/1.mp3"), ("Two", "https://cdn/2.mp3")])
    requests = []
    monkeypatch.setattr(feeds.http, "_request", lambda url, timeout=20, headers=None: requests.append(url) or body)

    ttls, parses = [], []
    real_fetch = feeds.http.fetch
    real_parse = feeds.parse

    def spy_fetch(url, timeout=25, headers=None, ttl=0):
        ttls.append(ttl)
        return real_fetch(url, timeout=timeout, headers=headers, ttl=ttl)

    def spy_parse(text):
        parses.append(text)
        return real_parse(text)

    monkeypatch.setattr(feeds.http, "fetch", spy_fetch)
    monkeypatch.setattr(feeds, "parse", spy_parse)

    assert feeds.count("f") == 2
    assert feeds.count("f") == 2
    assert len(requests) == 1, "the feed body must come from the TTL cache"
    assert ttls == [feeds.FEED_TTL]
    assert len(parses) == 1, "the count must be cached instead of re-parsed"
    assert feeds.count("missing") == 0


def test_feed_count_failures_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    _feed(monkeypatch)
    requests = []

    def dead_fetch(url, timeout=20, headers=None):
        requests.append(url)
        return None

    monkeypatch.setattr(feeds.http, "_request", dead_fetch)
    assert feeds.count("f") == 0
    assert feeds.count("f") == 0
    assert len(requests) == 2, "a temporary fetch failure must not stick for the TTL"
