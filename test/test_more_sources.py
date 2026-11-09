# -*- coding: utf-8 -*-
"""1.6.57 curated web sources: 20bensons, UK Raves, Mikus, Demodulated.

Everything is offline: page/feed fixtures are parsed directly, and every app
route under test has its network provider monkeypatched.
"""

import json
import os
import urllib.parse

import pytest

import app as app_module
from core import (bensons, cache, demodulated, drive, feeds, mikus, ukraves)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "more-sources")
SIGNED = ("https://download1346.mediafire.com/abc123/72aswcrx4hlw2sh/"
          "mix.mp3?token=abc")


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path, monkeypatch):
    """Keep parsed-page caches from leaking between tests."""
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def _json(name):
    return json.loads(_fixture(name))


# --------------------------------------------------------------------------- 20bensons


def test_20bensons_parses_all_234_links_in_order():
    rows = bensons.parse_homepage(_fixture("20bensons-home.html"))
    assert len(rows) == 234
    assert len({row["url"] for row in rows}) == 229  # five deliberate repeats
    assert rows[0]["title"] == "TOP BUZZ FANTAZIA NYE92 A-SIDE"
    assert rows[0]["year"] == "1992"
    assert rows[0]["event"] == "Fantazia"
    assert rows[0]["url"] == (
        "https://20bensons.com/mp3/TOP%20BUZZ%20FANTAZIA%20NYE92%20A-SIDE.MP3")
    # Spaces, apostrophes and upper-case .MP3 survive into a valid URL.
    slug = next(row for row in rows if "SLIPMAT @ FANTASIA '93" in row["title"])
    assert " " not in slug["url"] and "%20" in slug["url"] and "'" in slug["url"]
    assert slug["url"].endswith(".MP3")
    # The -SOUR suffix is stripped from the display title, not the URL.
    sour = next(row for row in rows if "KINGS OF THE JUNGLE" in row["title"])
    assert "SOUR" not in sour["title"]
    assert "SOUR" in sour["url"]
    assert sour["event"] == "Kings of the Jungle"


def test_20bensons_entries_hit_the_page_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bensons.http, "fetch",
        lambda *a, **k: calls.append(a) or _fixture("20bensons-home.html"))
    first = bensons.entries()
    second = bensons.entries()
    assert len(first) == len(second) == 234
    assert len(calls) == 1
    assert "234 old-skool rave MP3s" in bensons.summary(first)
    assert "1990–2005" in bensons.summary(first)


def test_20bensons_room_pages_at_40_and_marks_downloads(web, monkeypatch):
    rows = bensons.parse_homepage(_fixture("20bensons-home.html"))
    monkeypatch.setattr(app_module.bensons, "entries", lambda: rows)
    first = web.get("/api/20bensons").get_json()
    assert first["title"] == "20bensons.com"
    assert first["total"] == 234
    assert first["pager"]["pages"] == 6
    assert len(first["items"]) == 40
    item = first["items"][0]
    assert item["label"] == "TOP BUZZ FANTAZIA NYE92 A-SIDE"
    assert item["fact"] == "1992"
    assert "Fantazia" in item["sub"]
    assert item["play"].startswith("https://20bensons.com/mp3/")
    assert item["download"] == {"source": "20bensons"}
    assert item["fav"] == {"kind": "stream", "target": item["play"],
                           "title": item["label"]}
    assert item["art"] == "/static/art/20bensons.png"
    last = web.get("/api/20bensons?page=6").get_json()
    assert len(last["items"]) == 34
    assert last["pager"]["next"] is None


def test_20bensons_empty_source_is_graceful(web, monkeypatch):
    monkeypatch.setattr(app_module.bensons, "entries", lambda: [])
    payload = web.get("/api/20bensons").get_json()
    assert payload["items"] == []
    assert "unavailable" in payload["sub"].lower()


# --------------------------------------------------------------------------- UK Raves


def test_ukraves_parses_both_categories_and_the_next_page():
    downloads, has_next = ukraves.parse_page(
        _fixture("ukraves-downloads.html"), 1)
    other, _ = ukraves.parse_page(_fixture("ukraves-other-downloads.html"), 1)
    assert len(downloads) == len(other) == 20
    assert has_next is True
    first = downloads[0]
    assert first["id"] == "7524"
    assert first["title"] == "Doc Scott"
    assert first["event"].startswith("Dance Trance - Capital Punishment")
    assert first["date"] == "12 Nov 1993"
    assert first["go_url"] == "https://www.ukraves.co.uk/go/mix/7524"
    assert other[0]["subtitle"] == "Zone - Nosebleed Visions"


def test_ukraves_page_entries_combines_categories_and_caches(monkeypatch):
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append(url)
        return _fixture("ukraves-downloads.html" if "/mixes/downloads" in url
                        else "ukraves-other-downloads.html")

    monkeypatch.setattr(ukraves.http, "fetch", fake_fetch)
    rows, has_next = ukraves.page_entries(1)
    assert len(rows) == 40 and has_next is True
    assert len({row["id"] for row in rows}) == 40
    again, _ = ukraves.page_entries(1)
    assert len(again) == 40
    assert len(calls) == 2


def test_ukraves_describe_marks_youtube_and_mediafire(monkeypatch):
    rows = ukraves.parse_page(_fixture("ukraves-downloads.html"), 1)[0][:4]
    targets = {
        "7524": "https://www.youtube.com/watch?v=abc",
        "7523": "https://www.mediafire.com/file/id/name.MP3/file",
        "7522": "https://www.ukraves.co.uk/events/1",
    }
    monkeypatch.setattr(ukraves, "redirect_target",
                        lambda mix_id: targets.get(mix_id, ""))
    described = ukraves.describe_many(rows)
    kinds = [row["provider"]["kind"] for row in described]
    assert kinds[0] == "youtube"
    assert kinds[1] == "mediafire"
    assert kinds[2] == "external"
    assert kinds[3] == "unavailable"


def test_ukraves_mediafire_url_uses_the_generic_resolver(monkeypatch):
    page = "https://www.mediafire.com/file/hmfwgokmpwgdumt/mix.MP3/file"
    monkeypatch.setattr(ukraves, "redirect_target", lambda mix_id: page)
    monkeypatch.setattr(app_module.ezgarage, "resolve_page", lambda url: SIGNED)
    assert ukraves.mediafire_url("6948") == SIGNED
    monkeypatch.setattr(ukraves, "redirect_target",
                        lambda mix_id: "https://www.youtube.com/watch?v=x")
    with pytest.raises(ukraves.NotMediafire):
        ukraves.mediafire_url("7524")


def test_ukraves_room_lists_only_real_downloads(web, monkeypatch):
    rows = ukraves.parse_page(_fixture("ukraves-downloads.html"), 1)[0][:3]
    monkeypatch.setattr(app_module.ukraves, "page_entries",
                        lambda page: (rows, True))

    def describe(page_rows):
        first, second, third = page_rows
        return [
            dict(first, provider={"kind": "mediafire", "url": "https://www.mediafire.com/file/x/y.MP3/file"}),
            dict(second, provider={"kind": "youtube", "url": "https://www.youtube.com/watch?v=x"}),
            dict(third, provider={"kind": "unavailable", "url": ""}),
        ]

    monkeypatch.setattr(app_module.ukraves, "describe_many", describe)
    payload = web.get("/api/ukraves").get_json()
    assert payload["title"] == "UK Raves Downloads"
    assert payload["pager"]["total"] is None
    assert payload["pager"]["next"] == "/api/ukraves?page=2"
    assert "ukraves.co.uk" in payload["source"]
    assert payload["items"][0]["play"] == "/api/ukraves/7524/stream"
    assert payload["items"][0]["file"] == "/api/ukraves/7524/file"
    assert payload["items"][0]["download"] == {"source": "ukraves", "id": "7524"}
    assert payload["items"][0]["fav"] == {
        "kind": "stream", "target": "/api/ukraves/7524/stream",
        "title": payload["items"][0]["label"]}
    # YouTube/external hand-offs are excluded: no pop-up rows in a downloads room.
    assert len(payload["items"]) == 1


def test_ukraves_direct_mp3_rows_are_favouritable_not_downloadable(web, monkeypatch):
    rows = ukraves.parse_page(_fixture("ukraves-downloads.html"), 1)[0][:1]
    monkeypatch.setattr(app_module.ukraves, "page_entries", lambda page: (rows, False))
    direct = "https://files.ukraves.example/sets/doc-scott.mp3"
    monkeypatch.setattr(app_module.ukraves, "describe_many", lambda page_rows: [
        dict(page_rows[0], provider={"kind": "direct", "url": direct})])
    item = web.get("/api/ukraves").get_json()["items"][0]
    assert item["play"] == direct
    assert item["fav"] == {"kind": "stream", "target": direct, "title": item["label"]}
    # The direct host is not allowlisted, so no Download action is offered.
    assert "download" not in item


def test_ukraves_youtube_stream_route_is_a_handoff_not_a_500(web, monkeypatch):
    monkeypatch.setattr(app_module.ukraves, "mediafire_url",
                        lambda mix_id: (_ for _ in ()).throw(
                            ukraves.NotMediafire("not a Mediafire download", 409)))
    response = web.get("/api/ukraves/7524/stream")
    assert response.status_code == 409
    assert response.get_json()["code"] == "ukraves"


# --------------------------------------------------------------------------- Google Drive


def test_drive_confirm_form_and_file_id_helpers():
    url = drive.parse_confirm_form(_fixture("drive-warning.html"))
    parsed = urllib.parse.urlsplit(url)
    assert parsed.netloc == "drive.usercontent.google.com"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["id"] == ["1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7"]
    assert query["confirm"] == ["t"]
    assert drive.file_id("https://drive.google.com/file/d/%s/view?usp=sharing"
                         % "1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7") == \
        "1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7"
    assert drive.file_id("https://drive.google.com/open?id=1DCY4N3xMobup9AuZaHSXJZrffpQH9XZk") == \
        "1DCY4N3xMobup9AuZaHSXJZrffpQH9XZk"
    assert drive.file_id("nope") == ""


def test_drive_resolver_follows_the_virus_scan_form_and_caches(monkeypatch):
    drive.clear_memory()
    calls = []

    def fake_probe(url):
        calls.append(url)
        if len(calls) == 1:
            return 200, {"Content-Type": "text/html; charset=utf-8"}, url, \
                _fixture("drive-warning.html").encode("utf-8")
        return 206, {"Content-Type": "audio/mpeg"}, url, b"ID3"

    monkeypatch.setattr(drive, "_probe", fake_probe)
    url = drive.resolve("1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7")
    assert "confirm=t" in url and "uuid=" in url
    assert drive.resolve("1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7") == url
    assert len(calls) == 2


def test_drive_resolver_rejects_a_non_audio_file(monkeypatch):
    drive.clear_memory()

    def fake_probe(url):
        return 200, {"Content-Type": "text/plain"}, url, b"hello"

    monkeypatch.setattr(drive, "_probe", fake_probe)
    with pytest.raises(drive.DriveNotAudio):
        drive.resolve("1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7")


# --------------------------------------------------------------------------- Mikus Musik


def test_mikus_parses_blogger_posts_and_media():
    posts = mikus.parse_feed(_json("mikus-posts.json"))
    assert len(posts) == 100
    first = posts[0]
    assert first["id"] == "6755891406457697033"
    assert first["title"].startswith("House & Garage Classics")
    assert first["published"] == "2026-07-16"
    assert first["mixcloud"].startswith("https://www.mixcloud.com/mikus/")
    assert first["drive"][0]["id"] == "1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7"
    assert "download" in first["drive"][0]["title"].lower()
    assert first["youtube"] == ["https://www.youtube.com/watch?v=T0DcO0A5Zh8"]
    soundcloud = next(post for post in posts if post["soundcloud"])
    assert soundcloud["soundcloud"][0].startswith("https://soundcloud.com/")
    assert "w.soundcloud.com/player" in mikus.soundcloud_embed(
        soundcloud["soundcloud"][0])


def test_mikus_posts_hit_the_feed_once(monkeypatch):
    calls = []
    data = _json("mikus-posts.json")
    monkeypatch.setattr(mikus.http, "fetch_json",
                        lambda *a, **k: calls.append(a) or data)
    assert len(mikus.posts()) == 100
    assert len(mikus.posts()) == 100
    assert len(calls) == 1


def test_mikus_room_and_detail_expose_embeds_handoffs_and_drive(web, monkeypatch):
    posts = mikus.parse_feed(_json("mikus-posts.json"))
    monkeypatch.setattr(app_module.mikus, "posts", lambda: posts)
    room = web.get("/api/mikus").get_json()
    assert room["title"] == "Mikus Musik"
    assert room["total"] == 100
    assert room["items"][0]["open"] == "/api/mikus/6755891406457697033"
    assert "Mixcloud" in room["items"][0]["sub"]

    detail = web.get("/api/mikus/6755891406457697033").get_json()
    labels = [item["label"] for item in detail["items"]]
    assert "Mixcloud mix" in labels
    assert "YouTube video" in labels
    assert "Google Drive MP3" in labels
    mixcloud_row = next(item for item in detail["items"] if item["label"] == "Mixcloud mix")
    assert mixcloud_row["embed"].startswith("https://www.mixcloud.com/widget/iframe/")
    assert mixcloud_row["external"].startswith("https://www.mixcloud.com/")
    youtube_row = next(item for item in detail["items"] if item["label"] == "YouTube video")
    assert youtube_row["external"].startswith("https://www.youtube.com/")
    assert "download" not in youtube_row
    drive_row = next(item for item in detail["items"] if item["label"] == "Google Drive MP3")
    assert drive_row["play_via"].startswith("/api/mikus/drive/")
    assert drive_row["download"] == {"source": "mikus",
                                     "id": "1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7"}
    assert drive_row["fav"] == {
        "kind": "stream",
        "target": "/api/mikus/drive/1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7/stream",
        "title": "Google Drive MP3"}
    # Embed and hand-off rows have no stable audio target: no favourite either.
    assert "fav" not in mixcloud_row
    assert "fav" not in youtube_row
    assert "download" not in mixcloud_row


def test_mikus_direct_mp3_row_is_favouritable_and_downloadable(web, monkeypatch):
    posts = mikus.parse_feed(_json("mikus-posts.json"))
    monkeypatch.setattr(app_module.mikus, "posts", lambda: posts)
    detail = web.get("/api/mikus/5092857775336541428").get_json()
    row = next(item for item in detail["items"] if item["label"] == "Direct MP3")
    assert row["play"].startswith("http://www.terraincognita.co.uk/musik/")
    assert row["fav"] == {"kind": "stream", "target": row["play"],
                          "title": "Direct MP3"}
    assert row["download"] == {"source": "mikus"}


def test_mikus_drive_route_returns_a_direct_url(web, monkeypatch):
    monkeypatch.setattr(app_module.drive, "resolve", lambda file_id: SIGNED)
    payload = web.get("/api/mikus/drive/1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7").get_json()
    assert payload["url"] == SIGNED


def test_mikus_drive_stream_is_a_stable_same_origin_proxy(web, monkeypatch):
    monkeypatch.setattr(app_module.drive, "resolve", lambda file_id: SIGNED)
    seen = {}

    def fake_serve(target, options, requested_range=None, ref_override=None,
                   disposition=None):
        seen.update(target=target, options=options, requested_range=requested_range)
        return app_module.Response(b"ID3", mimetype="audio/mpeg")

    monkeypatch.setattr(app_module, "_serve_stream", fake_serve)
    response = web.get(
        "/api/mikus/drive/1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7/stream",
        headers={"Range": "bytes=0-3"})
    assert response.status_code == 200
    assert response.data == b"ID3"
    assert seen["target"] == SIGNED
    assert seen["requested_range"] == "bytes=0-3"
    assert "user-agent" in seen["options"]


# --------------------------------------------------------------------------- Demodulated


def test_demodulated_parses_sides_samples_and_ignores_zips():
    posts = demodulated.parse_feed(_fixture("demodulated-feed.xml"))
    assert len(posts) == 10
    first = posts[0]
    assert first["title"] == "Gulembo – The Best of The Cave"
    assert first["published"] == "2011-05-31"
    assert all(not track["url"].endswith(".zip") for track in first["tracks"])
    assert first["zips"] and first["zips"][0].endswith(".zip")
    hard = next(post for post in posts if post["title"].startswith("DJ Hardtripp"))
    titles = [track["title"] for track in hard["tracks"]]
    assert titles[:2] == ["Side A", "Side B"]
    assert titles[2:] == ["Sample", "Sample 2"]
    assert [track["sample"] for track in hard["tracks"]] == [False, False, True, True]


def test_demodulated_room_and_detail_mark_samples(web, monkeypatch):
    posts = demodulated.parse_feed(_fixture("demodulated-feed.xml"))
    monkeypatch.setattr(app_module.demodulated, "posts", lambda: posts)
    room = web.get("/api/demodulated").get_json()
    assert room["title"] == "Demodulated Mixtapes"
    assert room["total"] == 10
    assert room["items"][0]["open"] == "/api/demodulated/1"
    assert "sample" in room["items"][0]["sub"].lower()

    detail = web.get("/api/demodulated/3").get_json()
    assert detail["title"].startswith("DJ Hardtripp")
    sides = [item for item in detail["items"] if item.get("fact") != "SAMPLE"]
    samples = [item for item in detail["items"] if item.get("fact") == "SAMPLE"]
    assert len(sides) == 2 and len(samples) == 2
    # Full sides are downloadable; samples are playable/favouritable previews.
    assert all(item["download"] == {"source": "demodulated"} for item in sides)
    assert all("download" not in item for item in samples)
    assert all(item["fav"]["kind"] == "stream" for item in detail["items"])
    assert all(item["fav"]["target"] == item["play"] for item in detail["items"])
    assert all("music/mixsets/" in item["play"] for item in detail["items"])
    assert not any(item["play"].endswith(".zip") for item in detail["items"])


# --------------------------------------------------------------------------- podcasts


def test_podcast_registry_has_the_new_podomatic_feeds():
    by_id = {entry["id"]: entry for entry in feeds.registry()}
    assert by_id["rat-pack"]["url"] == "https://ratpack.podomatic.com/rss2.xml"
    assert by_id["dejavu-fm"]["url"] == "https://dejavufmpodcast.podomatic.com/rss2.xml"


def test_dejavu_feed_opts_into_a_larger_body_without_disk_caching(monkeypatch):
    entry = next(row for row in feeds.registry() if row["id"] == "dejavu-fm")
    assert entry["max_bytes"] >= 10 * 1024 * 1024
    assert entry["cache_body"] is False
    calls = {}

    def fake_fetch(url, **kwargs):
        calls.update(kwargs)
        return "<rss/>"

    monkeypatch.setattr(feeds.http, "fetch", fake_fetch)
    monkeypatch.setattr(feeds, "parse", lambda body: [{"title": "x", "url": "https://x/1.mp3"}])
    assert len(feeds.episodes("dejavu-fm")) == 1
    assert calls["max_bytes"] == entry["max_bytes"]
    assert calls["ttl"] == 0


def test_podcast_episodes_resolve_podomatic_downloads(web, monkeypatch):
    ratpack = feeds.parse(_fixture("ratpack-feed.xml"))
    dejavu = feeds.parse(_fixture("dejavu-podcast-feed.xml"))
    entries = [
        {"id": "rat-pack", "name": "Rat Pack", "url": "https://ratpack.podomatic.com/rss2.xml",
         "plot": ""},
        {"id": "dejavu-fm", "name": "DejaVu FM Podcast",
         "url": "https://dejavufmpodcast.podomatic.com/rss2.xml", "plot": ""},
    ]
    monkeypatch.setattr(app_module.feeds, "registry", lambda: entries)
    monkeypatch.setattr(app_module.feeds, "feed", lambda feed_id: next(
        (entry for entry in entries if entry["id"] == feed_id), None))
    monkeypatch.setattr(app_module.feeds, "episodes",
                        lambda feed_id: ratpack if feed_id == "rat-pack" else dejavu)

    payload = web.get("/api/podcast/rat-pack").get_json()
    assert payload["items"][0]["download"] == {"source": "podomatic"}
    assert payload["items"][0]["play"].startswith("https://ratpack.podomatic.com/")
    assert payload["items"][0]["fav"] == {
        "kind": "stream", "target": payload["items"][0]["play"],
        "title": payload["items"][0]["label"]}

    dejavu_payload = web.get("/api/podcast/dejavu-fm").get_json()
    first = dejavu_payload["items"][0]
    assert first["play"].startswith("https://dejavufmpodcast.podomatic.com/")
    assert "pdcn.co" not in first["play"]
    assert first["download"] == {"source": "podomatic"}
    assert first["fav"]["target"] == first["play"]


def test_latest_podomatic_episode_has_fav_and_download(web, monkeypatch):
    monkeypatch.setattr("app.latest.merge", lambda limit=60: [
        {"kind": "episode", "title": "Rat Pack 1",
         "url": "https://ratpack.podomatic.com/enclosure/one.mp3",
         "source": "Rat Pack", "when": None},
        {"kind": "episode", "title": "Other show",
         "url": "https://cdn.example/two.mp3", "source": "Other", "when": None},
    ])
    first, second = web.get("/api/latest").get_json()["items"]
    assert first["fav"] == {"kind": "stream", "target": first["play"],
                            "title": "Rat Pack 1"}
    assert first["download"] == {"source": "podomatic"}
    # Non-Podomatic enclosures are playable/favouritable but not downloadable.
    assert second["fav"]["target"] == "https://cdn.example/two.mp3"
    assert "download" not in second


# --------------------------------------------------------------------------- downloads + live


def test_download_allowlists_are_exact_hosts():
    allowed = app_module._download_allowed
    assert allowed("20bensons", "https://20bensons.com/mp3/A%20B.MP3")[0]
    assert not allowed("20bensons", "https://www.20bensons.com/mp3/x.mp3")[0]
    assert not allowed("20bensons", "https://20bensons.com/other/x.mp3")[0]
    assert allowed("demodulated", "http://www.demodulated.com/music/mixsets/x.mp3")[0]
    assert not allowed("demodulated", "http://www.demodulated.com/music/mixsets/x.zip")[0]
    assert not allowed("demodulated", "http://evil.test/music/mixsets/x.mp3")[0]
    assert allowed("podomatic", "https://ratpack.podomatic.com/enclosure/x.mp3")[0]
    assert not allowed("podomatic", "https://pdcn.co/e/x.mp3")[0]
    assert allowed("ukraves", "https://download1.mediafire.com/x/y.mp3")[0]
    assert not allowed("ukraves", "https://www.mediafire.com/file/x/y.mp3")[0]
    assert allowed("mikus", "https://drive.usercontent.google.com/download?id=x")[0]
    assert not allowed("mikus", "https://drive.google.com/file/d/x/view")[0]
    assert allowed("mikus", "http://www.terraincognita.co.uk/musik/one.mp3")[0]
    assert not allowed("mikus", "https://evil.example/musik/one.mp3")[0]
    assert not allowed("mikus", "http://www.terraincognita.co.uk/musik/one.zip")[0]


def test_ukraves_download_resolves_the_mediafire_page(web, monkeypatch):
    monkeypatch.setattr(app_module.ukraves, "mediafire_url", lambda mix_id: SIGNED)
    monkeypatch.setattr(app_module, "_open_stream",
                        lambda url, headers, deadline: _fake_stream(url))
    response = web.post("/api/downloads", data=json.dumps({
        "source": "ukraves", "source_id": "6948", "url": "/api/ukraves/6948/stream",
        "title": "Randall at Innovation",
    }), content_type="application/json")
    assert response.status_code == 200, response.get_data(as_text=True)
    item = response.get_json()["item"]
    assert item["label"] == "Randall at Innovation"
    listing = web.get("/api/downloads").get_json()["items"]
    assert listing[0]["sub"].endswith("UK Raves")


def test_mikus_download_uses_the_drive_view_as_stable_identity(web, monkeypatch):
    direct = "https://drive.usercontent.google.com/download?id=x&export=download"
    monkeypatch.setattr(app_module.drive, "resolve", lambda file_id: direct)
    monkeypatch.setattr(app_module, "_open_stream",
                        lambda url, headers, deadline: _fake_stream(url))
    response = web.post("/api/downloads", data=json.dumps({
        "source": "mikus", "source_id": "1tHRuSSMfNAdT1zicgYSrXegN0_RbvhS7",
        "url": direct, "title": "Mikus mix",
    }), content_type="application/json")
    assert response.status_code == 200, response.get_data(as_text=True)
    # The stored/dedupe URL is the stable Drive view, not the expiring signed URL.
    listing = web.get("/api/downloads").get_json()["items"]
    assert listing[0]["label"] == "Mikus mix"
    assert listing[0]["sub"].endswith("Mikus Musik")


def test_mikus_direct_mp3_download_is_allowlisted(web, monkeypatch):
    url = "http://www.terraincognita.co.uk/musik/One%20Last%20Session.mp3"
    monkeypatch.setattr(app_module, "_open_stream",
                        lambda target, headers, deadline: _fake_stream(target))
    response = web.post("/api/downloads", data=json.dumps({
        "source": "mikus", "url": url, "title": "One Last Session",
    }), content_type="application/json")
    assert response.status_code == 200, response.get_data(as_text=True)
    listing = web.get("/api/downloads").get_json()["items"]
    assert listing[0]["label"] == "One Last Session"
    assert listing[0]["sub"].endswith("Mikus Musik")
    # A saved download is itself a track row with a stable same-origin favourite.
    assert listing[0]["fav"] == {"kind": "stream", "target": listing[0]["play"],
                                 "title": "One Last Session"}


def test_live_room_includes_the_two_new_stations(web):
    payload = web.get("/api/live").get_json()
    by_label = {item["label"]: item for item in payload["items"]}
    assert by_label["DejaVu FM"]["play"] == "https://dejavufm.radioca.st/stream"
    assert by_label["DejaVu FM"]["art"] == "/static/art/dejavu.png"
    assert by_label["OSA Radio"]["play"] == "https://osa.radioca.st/stream"
    assert by_label["OSA Radio"]["art"] == "/static/art/osa.png"
    assert "duration" not in by_label["DejaVu FM"]
    assert by_label["DejaVu FM"]["fav"]["target"] == by_label["DejaVu FM"]["play"]
    # Live streams are never download targets.
    assert all("download" not in item for item in payload["items"])


def test_new_rooms_are_in_the_menu_and_hubs(web):
    menu = web.get("/api/menu").get_json()
    by_id = {row["id"]: row for row in menu["sections"]}
    for section_id, path in (("20bensons", "/api/20bensons"),
                             ("ukraves", "/api/ukraves"),
                             ("mikus", "/api/mikus"),
                             ("demodulated", "/api/demodulated")):
        assert by_id[section_id]["path"] == path
        assert by_id[section_id]["icon"].startswith("/static/art/")
    packs = {group["id"]: group for group in menu["rail"]}["packs"]
    assert ["20bensons", "ukraves"] == [child["id"] for child in packs["children"]][4:6]
    shows = {group["id"]: group for group in menu["rail"]}["shows"]
    assert ["mikus", "demodulated"] == [child["id"] for child in shows["children"]][-2:]


class _FakeResponse(object):
    def __init__(self, body=b"ID3" + b"a" * 1000):
        self.status = 200
        self._body = body
        self._offset = 0

    def getheader(self, name):
        return {"content-type": "audio/mpeg",
                "content-length": str(len(self._body))}.get((name or "").lower())

    def read(self, size=-1):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeConnection(object):
    def close(self):
        pass


def _fake_stream(url):
    return _FakeConnection(), _FakeResponse(), url


def test_feed_episodes_load_once_and_share_the_read_only_list(monkeypatch):
    calls = []

    def load(entry):
        calls.append(entry["id"])
        return [{"title": "Show", "url": "https://cdn.example/1.mp3"}]

    monkeypatch.setattr(feeds, "_load_episodes", load)
    first = feeds.episodes("dejavu-fm")
    second = feeds.episodes("dejavu-fm")
    assert calls == ["dejavu-fm"], "a warm feed must not be re-read"
    assert first == second == [{"title": "Show", "url": "https://cdn.example/1.mp3"}]
    assert feeds.episodes("dejavu-fm", limit=1) == [{"title": "Show", "url": "https://cdn.example/1.mp3"}]
    assert len(feeds.episodes("dejavu-fm")) == 1
