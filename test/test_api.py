# -*- coding: utf-8 -*-
"""API route behaviour, validation, pagination and error shapes.

All providers are stubbed; nothing here touches the network.
"""

import json

import pytest

import app


# --------------------------------------------------------------------------- structure


def test_healthz(web, tmp_path):
    response = web.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["version"] == app.APP_VERSION
    assert payload["data_writable"] is True


def test_healthz_with_read_only_data_dir(web, tmp_path, monkeypatch):
    target = tmp_path / "nope"
    target.write_text("not a directory")
    monkeypatch.setattr("app.DATA_DIR", str(target))
    payload = web.get("/healthz").get_json()
    assert payload["ok"] is True
    assert payload["data_writable"] is False


def test_menu_shape(web):
    payload = web.get("/api/menu").get_json()
    ids = [section["id"] for section in payload["sections"]]
    assert ids[0] == "latest"
    assert "favourites" in ids and "search" in ids
    assert len(ids) == len(app.SECTIONS)
    assert "ukg" in ids
    assert payload["site"]["title"] == "Rave Player"
    for section in payload["sections"]:
        assert set(section) == {"id", "label", "icon", "sub", "path"}
        assert section["icon"].startswith("/static/art/")
        assert section["path"].startswith("/api/")


def test_manifest(web):
    response = web.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/manifest+json")
    assert response.get_json()["start_url"] == "/"


def test_index_page_has_app_shell(web):
    response = web.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for marker in ('id="list"', 'id="player"', 'id="audio"', 'id="search-input"',
                   'id="queue"', 'id="p-airplay"', 'aria-live="polite"',
                   'rel="manifest"', 'playsinline', 'webkit-playsinline',
                   'x-webkit-airplay="allow"'):
        assert marker in body
    csp = response.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_csp_allows_the_embed_players_without_widening_anything_else(web):
    """The Mixcloud/SoundCloud sections frame the providers' own widgets.

    `frame-src` falls back to `default-src 'self'`, so without this directive
    the iframes in the embed panel are refused (audit risk 1).  Keep the list
    to the two widget hosts and leave every other directive unchanged.
    """
    csp = web.get("/").headers["Content-Security-Policy"]
    assert "frame-src https://www.mixcloud.com https://player-widget.mixcloud.com https://w.soundcloud.com" in csp
    for directive in ("default-src 'self'", "img-src 'self' data: https: http:",
                      "media-src 'self'", "script-src 'self'", "style-src 'self'",
                      "connect-src 'self'", "manifest-src 'self'",
                      "frame-ancestors 'self'", "base-uri 'none'", "form-action 'self'"):
        assert directive in csp, "CSP lost %r" % directive
    frame_src = [part.strip() for part in csp.split(";") if part.strip().startswith("frame-src")]
    assert frame_src == ["frame-src https://www.mixcloud.com https://player-widget.mixcloud.com https://w.soundcloud.com"]


def test_csp_is_identical_on_the_static_asset_responses(web):
    """The app deliberately applies the same policy to /static/* responses;
    keep the two in sync so the header cannot drift by route."""
    page = web.get("/").headers["Content-Security-Policy"]
    asset = web.get("/static/app.js").headers["Content-Security-Policy"]
    assert asset == page
    assert "frame-src https://www.mixcloud.com https://player-widget.mixcloud.com https://w.soundcloud.com" in asset


def test_api_404_is_json(web):
    response = web.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.is_json
    assert response.get_json() == {"error": "not found"}


def test_api_405_is_json(web):
    response = web.post("/api/menu")
    assert response.status_code == 405
    assert response.get_json() == {"error": "method not allowed"}


def test_api_500_is_json_and_does_not_leak(web, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr("app.archive.search", boom)
    response = web.get("/api/packs/rave")
    assert response.status_code == 500
    assert response.get_json() == {"error": "internal error"}
    assert "secret internal detail" not in response.get_data(as_text=True)


def test_html_500_stays_html(web, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr("app.render_template", boom)
    response = web.get("/")
    assert response.status_code == 500
    assert response.mimetype == "text/html"


# --------------------------------------------------------------------------- latest


def test_latest_mixed_sources(web, monkeypatch):
    import datetime

    monkeypatch.setattr("app.latest.merge", lambda limit=60: [
        {"kind": "pack", "title": "Dreamscape 10", "identifier": "dreamscape-10",
         "source": "Rave Tape Packs", "when": datetime.datetime(2026, 1, 2)},
        {"kind": "episode", "title": "Jungle Lab 42", "url": "https://cdn.example/ep42.mp3",
         "source": "Jungle Lab", "when": None},
        {"kind": "dnb", "title": "Valis - Session", "id": "58124", "source": "DNB Radio",
         "when": None},
        {"kind": "unknown", "title": "ignored"},
        {"kind": "pack", "title": "", "identifier": "no-title"},
    ])
    payload = web.get("/api/latest").get_json()
    assert payload["title"] == "Latest"
    assert len(payload["items"]) == 3
    pack, episode, dnb = payload["items"]
    assert pack["open"] == "/api/item/dreamscape-10"
    assert pack["fav"] == {"kind": "pack", "target": "dreamscape-10", "title": "Dreamscape 10"}
    assert episode["play"] == "https://cdn.example/ep42.mp3"
    assert dnb["play"] is None and dnb["play_via"] == "/api/dnb/58124"


def test_latest_provider_failure_is_502(web, monkeypatch):
    def boom(limit=60):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.latest.merge", boom)
    response = web.get("/api/latest")
    assert response.status_code == 502
    assert response.get_json()["error"] == "could not load latest"


# --------------------------------------------------------------------------- filters


def test_filters_list(web):
    payload = web.get("/api/filters").get_json()
    ids = [item["open"] for item in payload["items"]]
    assert "/api/filter/kool" in ids
    assert all(item["sub"] for item in payload["items"])


def test_filter_kool_first_page_has_live_row(web, archive_search):
    response = web.get("/api/filter/kool")
    payload = response.get_json()
    assert payload["items"][0]["play_via"] == "/api/filter/kool/live"
    assert payload["items"][1]["open"] == "/api/item/pack-1"
    assert payload["next"] == "/api/filter/kool?page=2"
    assert archive_search["calls"][-1]["page"] == 1
    assert archive_search["calls"][-1]["rows"] == 40


def test_filter_later_pages_have_no_live_row(web, archive_search):
    payload = web.get("/api/filter/kool?page=2").get_json()
    assert payload["items"][0]["open"] == "/api/item/pack-1"


def test_filter_unknown_is_404(web):
    response = web.get("/api/filter/nope")
    assert response.status_code == 404
    assert response.get_json() == {"error": "unknown filter"}


@pytest.mark.parametrize("page", ["abc", "-3", "0", ""])
def test_filter_bad_page_falls_back_to_one(web, archive_search, page):
    web.get("/api/filter/kool?page=%s" % page)
    assert archive_search["calls"][-1]["page"] == 1


def test_filter_page_is_clamped(web, archive_search):
    payload = web.get("/api/filter/kool?page=99999999").get_json()
    assert archive_search["calls"][-1]["page"] == 500
    assert "next" not in payload


def test_filter_last_page_has_no_next(web, archive_search):
    archive_search["state"]["total"] = 40
    payload = web.get("/api/filter/kool?page=1").get_json()
    assert "next" not in payload


def test_filter_live(web, monkeypatch):
    # curated stations take priority; force the radio-browser fallback here
    monkeypatch.setattr("app.radio.curated", lambda name: [])
    monkeypatch.setattr("app.radio.stations_by_name", lambda name, limit=20: [
        {"name": "Kool FM", "countrycode": "GB", "codec": "MP3", "bitrate": 128,
         "url_resolved": "http://stream.example/kool", "favicon": ""},
    ])
    payload = web.get("/api/filter/kool/live").get_json()
    assert payload["title"] == "Kool FM — live"
    assert payload["items"][0]["play"] == "http://stream.example/kool"
    assert payload["items"][0]["sub"] == "GB · MP3 · 128 kbps"


def test_filter_live_curated(web):
    payload = web.get("/api/filter/kool/live").get_json()
    assert payload["items"][0]["play"].endswith("/proxy/kool/stream")
    payload = web.get("/api/filter/rinse/live").get_json()
    assert payload["items"][0]["play"].endswith("/proxy/rinse_uk/stream")


def test_filter_live_unknown(web):
    assert web.get("/api/filter/nope/live").status_code == 404


# --------------------------------------------------------------------------- radio


def test_live_groups(web):
    payload = web.get("/api/live").get_json()
    assert [item["open"] for item in payload["items"]][:2] == ["/api/live/oldskool", "/api/live/jungle"]


def test_live_tag_skips_stations_without_stream(web, monkeypatch):
    monkeypatch.setattr("app.radio.stations", lambda tag, limit=100: [
        {"name": "Good", "url_resolved": "http://a.example/1", "codec": "MP3"},
        {"name": "Broken", "url_resolved": ""},
        "not a dict",
    ])
    payload = web.get("/api/live/jungle").get_json()
    assert [item["label"] for item in payload["items"]] == ["Good"]


def test_live_unknown_group(web):
    response = web.get("/api/live/polka")
    assert response.status_code == 404
    assert response.get_json() == {"error": "unknown station group"}


# --------------------------------------------------------------------------- packs + items


def test_packs_sections(web):
    payload = web.get("/api/packs").get_json()
    ids = [item["open"] for item in payload["items"]]
    assert "/api/packs/rave" in ids and "/api/packs/tapepacks" in ids


def test_packs_list_skips_docs_without_identifier(web, archive_search):
    archive_search["state"]["docs"] = [
        {"identifier": "one", "title": "One"},
        {"title": "no identifier"},
        "not a dict",
    ]
    payload = web.get("/api/packs/rave").get_json()
    assert [item["open"] for item in payload["items"]] == ["/api/item/one"]


def test_packs_unknown(web):
    assert web.get("/api/packs/nope").get_json() == {"error": "unknown section"}


def test_item_tracks(web, monkeypatch):
    monkeypatch.setattr("app.archive.audio_files", lambda identifier: [
        {"title": "Side A", "url": "https://archive.org/download/x/a.mp3", "length": "3725"},
        {"title": "Side B", "url": "https://archive.org/download/x/b.mp3", "length": None},
    ])
    payload = web.get("/api/item/x").get_json()
    assert payload["title"] == "x"
    assert payload["items"][0]["sub"] == "1:02:05"
    assert payload["items"][1]["sub"] == ""
    assert payload["items"][0]["fav"]["kind"] == "stream"


def test_item_without_audio_is_404(web, monkeypatch):
    monkeypatch.setattr("app.archive.audio_files", lambda identifier: [])
    response = web.get("/api/item/empty")
    assert response.status_code == 404
    assert response.get_json() == {"error": "no playable audio for that item"}


def test_item_with_slash_is_404(web):
    assert web.get("/api/item/a/b").status_code == 404


# --------------------------------------------------------------------------- podcasts


def test_podcasts(web, monkeypatch):
    monkeypatch.setattr("app.feeds.registry", lambda: [
        {"id": "jungle-lab", "name": "Jungle Lab", "url": "https://x/feed", "plot": ""},
    ])
    monkeypatch.setattr("app.feeds.count", lambda feed_id: 1)
    payload = web.get("/api/podcasts").get_json()
    assert payload["items"][0]["sub"] == "1 episodes"
    assert payload["items"][0]["open"] == "/api/podcast/jungle-lab"


def test_podcasts_uses_cached_counts_without_downloading_episode_lists(web, monkeypatch):
    """Regression: /api/podcasts fetched every feed (limit 200) just to count."""
    monkeypatch.setattr("app.feeds.registry", lambda: [
        {"id": "one", "name": "Feed One", "url": "https://x/1", "plot": ""},
        {"id": "two", "name": "Feed Two", "url": "https://x/2", "plot": ""},
    ])
    monkeypatch.setattr("app.feeds.count", lambda feed_id: {"one": 5, "two": 0}[feed_id])

    def boom(*args, **kwargs):
        raise AssertionError("podcasts must not download full episode lists to count")

    monkeypatch.setattr("app.feeds.episodes", boom)
    payload = web.get("/api/podcasts").get_json()
    assert payload["title"] == "Podcasts & Mixes"
    assert [item["label"] for item in payload["items"]] == ["Feed One", "Feed Two"]
    assert [item["sub"] for item in payload["items"]] == ["5 episodes", "0 episodes"]


def test_podcast_episodes(web, monkeypatch):
    monkeypatch.setattr("app.feeds.feed", lambda feed_id: {"id": "f", "name": "Feed"} if feed_id == "f" else None)
    monkeypatch.setattr("app.feeds.episodes", lambda feed_id, limit=60: [
        {"title": "Episode 1", "url": "https://cdn/ep1.mp3", "published": "Mon, 01 Jan 2026 10:00:00 +0000"},
        {"title": "", "url": ""},
    ])
    payload = web.get("/api/podcast/f").get_json()
    assert payload["title"] == "Feed"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["sub"] == "Mon, 01 Jan 2026"


def test_podcast_unknown(web):
    assert web.get("/api/podcast/nope").status_code == 404


# --------------------------------------------------------------------------- dnb


def test_dnb_calls_provider_once(web, monkeypatch):
    calls = []

    def mixes():
        calls.append(1)
        return [
            {"id": "1", "title": "Show One", "date": "2026-01-01"},
            {"id": "2", "title": "Show Two", "date": ""},
            {"title": "no id"},
        ]

    monkeypatch.setattr("app.dnbradio.mixes", mixes)
    payload = web.get("/api/dnb").get_json()
    assert len(calls) == 1
    assert [item["play_via"] for item in payload["items"]] == ["/api/dnb/1", "/api/dnb/2"]


def test_dnb_resolve_is_cached(web, monkeypatch):
    calls = []
    monkeypatch.setattr("app.dnbradio.stream_url", lambda mix_id: calls.append(mix_id) or "https://cdn/x.mp3")
    first = web.get("/api/dnb/58124").get_json()
    second = web.get("/api/dnb/58124").get_json()
    assert first == {"url": "https://cdn/x.mp3"}
    assert second == first
    assert calls == ["58124"]


def test_dnb_resolve_failure(web, monkeypatch):
    monkeypatch.setattr("app.dnbradio.stream_url", lambda mix_id: "")
    assert web.get("/api/dnb/58124").status_code == 404


@pytest.mark.parametrize("mix_id", ["abc", "12/34", "-1"])
def test_dnb_rejects_bad_ids(web, mix_id):
    assert web.get("/api/dnb/%s" % mix_id).status_code == 404


# --------------------------------------------------------------------------- years


def test_years(web):
    import datetime

    payload = web.get("/api/years").get_json()
    years = [int(item["label"]) for item in payload["items"]]
    assert years[0] == datetime.date.today().year
    assert years[-1] == 1987


@pytest.mark.parametrize("year", ["1986", "0", "9999"])
def test_year_out_of_range(web, year):
    response = web.get("/api/year/%s" % year)
    assert response.status_code == 404
    assert response.get_json() == {"error": "unknown year"}


def test_year_listing(web, archive_search):
    payload = web.get("/api/year/1993").get_json()
    assert payload["title"] == "1993"
    assert archive_search["calls"][-1]["query"].startswith("collection:ravetapes")
    assert "1993-01-01" in archive_search["calls"][-1]["query"]


# --------------------------------------------------------------------------- legacy


def test_legacy_sites(web):
    payload = web.get("/api/legacy").get_json()
    assert [item["open"] for item in payload["items"]] == [
        "/api/legacy/ravetapepacks", "/api/legacy/jungletapes", "/api/legacy/toronto"]


def test_legacy_ravetapepacks_list(web, monkeypatch):
    monkeypatch.setattr("app.legacy.packs", lambda: [
        {"key": "ExposureVol1", "name": "Exposure Vol 1", "tracks": [{"name": "A", "url": "u"}]},
        {"name": "no key", "tracks": []},
        "junk",
    ])
    payload = web.get("/api/legacy/ravetapepacks").get_json()
    assert payload["items"][0]["label"] == "Exposure Vol 1 (1 tapes)"
    assert payload["items"][0]["open"] == "/api/legacy/ravetapepacks/ExposureVol1"


def test_legacy_ravetapepacks_tracks_keep_referer_suffix(web, monkeypatch):
    monkeypatch.setattr("app.legacy.packs", lambda: [
        {"key": "ExposureVol1", "name": "Exposure", "tracks": [
            {"name": "Bradley M", "url": "https://www.ravetapepacks.com/music/E/Bradley_M.mp3"},
        ]},
    ])
    payload = web.get("/api/legacy/ravetapepacks/ExposureVol1").get_json()
    play = payload["items"][0]["play"]
    assert play.endswith("|Referer=https://www.ravetapepacks.com/&User-Agent=Mozilla/5.0")
    assert payload["items"][0]["fav"]["target"] == play


def test_legacy_unknown_pack(web, monkeypatch):
    monkeypatch.setattr("app.legacy.packs", lambda: [])
    assert web.get("/api/legacy/ravetapepacks/nope").status_code == 404


def test_legacy_jungletapes_show(web, monkeypatch):
    monkeypatch.setattr("app.legacy.jungle_tracks", lambda key: [
        {"name": "Show 1", "url": "https://jungletapes.com/a.mp3"}])
    payload = web.get("/api/legacy/jungletapes/some-show").get_json()
    assert payload["title"] == "some show"
    assert payload["items"][0]["play"] == "https://jungletapes.com/a.mp3"


def test_legacy_jungletapes_empty_show_is_404(web, monkeypatch):
    monkeypatch.setattr("app.legacy.jungle_tracks", lambda key: [])
    assert web.get("/api/legacy/jungletapes/dead").status_code == 404


def test_legacy_toronto(web, monkeypatch):
    monkeypatch.setattr("app.legacy.toronto_tracks", lambda: [
        {"name": "Mixtape 1", "url": "https://tr.example/1.mp3"}])
    payload = web.get("/api/legacy/toronto").get_json()
    assert payload["items"][0]["label"] == "Mixtape 1"


# --------------------------------------------------------------------------- search


def test_search_without_query(web):
    payload = web.get("/api/search").get_json()
    assert payload == {"title": "Search", "items": []}


def test_search_uses_collection_first(web, archive_search):
    web.get("/api/search?q=Kool")
    query = archive_search["calls"][0]["query"]
    assert query.startswith("Kool AND collection:ravetapes")


def test_search_widens_when_thin(web, archive_search, monkeypatch):
    calls = []

    def fake_search(query, page=1, rows=50, sort="downloads desc"):
        calls.append(query)
        if query.startswith("tape AND mediatype:audio"):
            return [{"identifier": "two", "title": "Two"}], 500
        return [{"identifier": "one", "title": "One"}], 2

    monkeypatch.setattr("app.archive.search", fake_search)
    monkeypatch.setattr("app.mixcloud.search", lambda *a, **k: [])
    payload = web.get("/api/search?q=tape").get_json()
    assert [item["open"] for item in payload["items"]] == ["/api/item/one", "/api/item/two"]
    assert "next" not in payload
    assert len(calls) == 2


def test_search_query_is_capped(web, archive_search):
    web.get("/api/search?q=" + "a" * 500)
    query = archive_search["calls"][0]["query"]
    assert query.split(" AND ")[0] == "a" * 200


def test_search_pagination(web, archive_search):
    archive_search["state"]["total"] = 200
    payload = web.get("/api/search?q=kool&page=2").get_json()
    assert payload["next"] == "/api/search?q=kool&page=3"
    assert archive_search["calls"][0]["page"] == 2


# --------------------------------------------------------------------------- misc


def test_human_duration():
    import app as app_module

    assert app_module._human_duration(0) == "0:00"
    assert app_module._human_duration("3725") == "1:02:05"
    assert app_module._human_duration(None) == ""
    assert app_module._human_duration("abc") == ""


def test_kodi_target_split():
    import app as app_module

    url = "https://x.example/a.mp3|Referer=https://x.example/&User-Agent=Mozilla/5.0"
    target, options = app_module._split_kodi_target(url)
    assert target == "https://x.example/a.mp3"
    assert options["referer"] == "https://x.example/"
    assert options["user-agent"] == "Mozilla/5.0"
    assert app_module._split_kodi_target("https://x.example/a.mp3") == ("https://x.example/a.mp3", {})
    # A pipe that is part of the URL is left alone.
    assert app_module._split_kodi_target("https://x.example/a|b") == ("https://x.example/a|b", {})


def test_clean_range():
    import app as app_module

    assert app_module._clean_range("bytes=0-1000") == "bytes=0-1000"
    assert app_module._clean_range(" bytes=100- ") == "bytes=100-"
    assert app_module._clean_range("bytes=0-1, 20-30") == ""
    assert app_module._clean_range("bytes=0-1\r\nX-Evil: 1") == ""
    assert app_module._clean_range("items=0-1") == ""


def test_request_target_encodes_unicode():
    import app as app_module

    assert app_module._request_target("https://x.example/café.mp3") == "/caf%C3%A9.mp3"
    assert app_module._request_target("https://x.example/a%20b.mp3?x=1") == "/a%20b.mp3?x=1"
