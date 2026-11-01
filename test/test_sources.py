# -*- coding: utf-8 -*-
"""Offline tests for the 1.6.14 mirror sources (Moxser, Hardscore, Rave Archive,
GetDarker) and the curated Pirate Radio Archives.

No test touches the network: every adapter separates fetching from parsing, so
these monkeypatch ``core.fetch.fetch`` / ``core.fetch.fetch_json`` with small
fixtures and exercise the real parsing + routes.
"""

import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MOX_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://moxser.com/sitemaps-1-section-artist-1-sitemap-p1.xml</loc></sitemap>
  <sitemap><loc>https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p1.xml</loc></sitemap>
  <sitemap><loc>https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p2.xml</loc></sitemap>
</sitemapindex>"""

MOX_P1 = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url>
    <loc>https://moxser.com/mixes/alpha-show-1062-26-october-2026</loc>
    <lastmod>2026-10-26T17:06:25+00:00</lastmod>
    <image:image><image:loc>https://moxser.com/assets/uploads/primary/Alpha-Show.jpg</image:loc>
      <image:title>Alpha+Show+1062+26+October+2026</image:title></image:image>
  </url>
  <url>
    <loc>https://moxser.com/mixes/beta-sessions-12-01-october-2026</loc>
    <lastmod>2026-10-10T10:00:00+00:00</lastmod>
  </url>
</urlset>"""

MOX_P2 = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://moxser.com/mixes/old-show-900-01-january-2025</loc>
    <lastmod>2025-01-01T00:00:00+00:00</lastmod></url>
</urlset>"""

MOX_PAGE = """<!doctype html><html><body>
<h1 class="font-display mb-6">Alpha Show 1062 (26 October 2026)</h1>
<div class="player"></div>
<script>window.track = { path: 'https://enceladus.feralhosting.com/moxser/2026/90104/Alpha%20Show.mp3' };</script>
</body></html>"""

HARDSCORE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Hardscore.com</title>
  <item>
    <title>Force of One Crew &#8211; Weekend Rush 92.3 FM [June 1993]</title>
    <link>https://www.hardscore.com/?p=11680</link>
    <pubDate>Wed, 22 Nov 2023 10:00:00 +0000</pubDate>
    <content:encoded><![CDATA[<a href="https://hardscore.com/mixes/WR/force.mp3">download</a>]]></content:encoded>
  </item>
  <item>
    <title>Event: RollDaBeats at Lockside</title>
    <link>https://www.hardscore.com/?p=1250</link>
    <pubDate>Thu, 08 Oct 2009 13:35:22 +0000</pubDate>
  </item>
</channel></rss>"""

HARDSCORE_EVENTS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Hardscore Events</title>
  <item><title>Oldskool Crew Live Friday 16th December 2011</title>
    <link>https://www.hardscore.com/?p=2238</link>
    <pubDate>Thu, 15 Dec 2011 18:50:33 +0000</pubDate></item>
</channel></rss>"""

RAVEARCHIVE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Rave Archive UK</title>
  <item>
    <title>Dreamscape 7 &#8216;Back to our Roots&#8217; &#8211; The Sanctuary &#8211; 26.11.93</title>
    <link>https://rave-archive.com/dreamscape-7-back-to-our-roots-the-sanctuary-26-11-93/</link>
    <pubDate>Thu, 21 Nov 2024 23:16:51 +0000</pubDate>
    <category>1993</category><category>Dreamscape</category><category>DJ Slipmatt</category>
  </item>
</channel></rss>"""

RAVEARCHIVE_PAGE = """<html><body>
<script>var player = mixcloudPlayer('https://www.mixcloud.com/Dizzyuk/ltj-bukem-dreamscape-7-the-sanctuary-261193/')</script>
</body></html>"""

GETDARKER_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>GetDarker</title>
  <item>
    <title>Skream &amp; Benga &#8211; FWD &amp; Rinse &#8211; 20.11.2009</title>
    <link>https://getdarker.com/audio/sets/skream-benga-fwd-rinse-matter-20-11-2009/</link>
    <pubDate>Wed, 20 Nov 2024 20:56:00 +0000</pubDate>
    <content:encoded><![CDATA[<iframe src="https://www.mixcloud.com/widget/iframe/?feed=https%3A%2F%2Fwww.mixcloud.com%2Fgetdarker%2Fskream-benga-fwd-rinse-matter-20112009%2F&hide_cover=1"></iframe>]]></content:encoded>
  </item>
  <item>
    <title>J Da Flex &#8211; Underground Knowledge (YouTube only)</title>
    <link>https://getdarker.com/audio/sets/j-da-flex-underground-knowledge/</link>
    <pubDate>Sat, 05 Jul 2025 18:26:40 +0000</pubDate>
  </item>
</channel></rss>"""


def _route(monkeypatch, mapping):
    """Patch fetch.fetch with a URL -> body mapping (None = failure)."""
    from core import fetch

    def fake_fetch(url, timeout=None, headers=None, ttl=0):
        return mapping.get(url)

    monkeypatch.setattr(fetch, "fetch", fake_fetch)
    return fake_fetch


def _route_json(monkeypatch, mapping):
    from core import fetch

    monkeypatch.setattr(fetch, "fetch_json",
                        lambda url, timeout=None, headers=None, ttl=0: mapping.get(url))


# --------------------------------------------------------------------------- Moxser


def test_moxser_lists_newest_mixes_from_the_sitemaps(monkeypatch):
    from core import moxser

    monkeypatch.setattr(moxser, "_throttle", lambda: None)
    _route(monkeypatch, {
        "https://moxser.com/sitemaps-1-sitemap.xml": MOX_INDEX,
        "https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p1.xml": MOX_P1,
        "https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p2.xml": MOX_P2,
    })
    mixes = moxser.mixes(limit=10)
    assert [mix["slug"] for mix in mixes] == [
        "alpha-show-1062-26-october-2026",
        "beta-sessions-12-01-october-2026",
        "old-show-900-01-january-2025",
    ]
    assert mixes[0]["title"] == "Alpha Show 1062 26 October 2026"
    assert mixes[0]["date"] == "2026-10-26"  # sitemap-only rows: lastmod date
    assert mixes[0]["flyer"].endswith("Alpha-Show.jpg")
    # The artist sitemap must be ignored.
    assert all("/mixes/" in mix["url"] for mix in mixes)


def test_moxser_detail_resolves_title_date_and_direct_mp3(monkeypatch):
    from core import moxser

    monkeypatch.setattr(moxser, "_throttle", lambda: None)
    _route(monkeypatch, {
        "https://moxser.com/sitemaps-1-sitemap.xml": MOX_INDEX,
        "https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p1.xml": MOX_P1,
        "https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p2.xml": MOX_P2,
        "https://moxser.com/mixes/alpha-show-1062-26-october-2026": MOX_PAGE,
    })
    mix = moxser.detail("alpha-show-1062-26-october-2026")
    assert mix["title"] == "Alpha Show 1062 (26 October 2026)"
    assert mix["date"] == "26 October 2026"
    assert mix["url"] == ("https://enceladus.feralhosting.com/moxser/2026/90104/"
                          "Alpha%20Show.mp3")


def test_moxser_fails_soft_when_the_site_is_down(monkeypatch):
    from core import moxser

    monkeypatch.setattr(moxser, "_throttle", lambda: None)
    _route(monkeypatch, {})
    assert moxser.mixes() == []
    assert moxser.detail("alpha-show-1062-26-october-2026") is None


# --------------------------------------------------------------------------- Hardscore


def test_hardscore_mixes_keep_direct_audio_and_events_link_out(monkeypatch):
    from core import hardscore

    _route(monkeypatch, {
        hardscore.FEED: HARDSCORE_FEED,
        hardscore.EVENTS_FEED: HARDSCORE_EVENTS,
    })
    mixes = hardscore.mixes()
    assert mixes[0]["media"] == "https://hardscore.com/mixes/WR/force.mp3"
    assert mixes[1]["media"] == ""
    events = hardscore.events()
    assert events[0]["link"] == "https://www.hardscore.com/?p=2238"


def test_hardscore_fails_soft(monkeypatch):
    from core import hardscore

    _route(monkeypatch, {})
    assert hardscore.mixes() == [] and hardscore.events() == []


# --------------------------------------------------------------------------- Rave Archive UK


def test_ravearchive_releases_read_year_and_djs_from_categories(monkeypatch):
    from core import ravearchive

    _route(monkeypatch, {ravearchive.FEED: RAVEARCHIVE_FEED})
    releases = ravearchive.releases()
    assert releases[0]["year"] == "1993"
    assert releases[0]["djs"] == ["Dreamscape", "DJ Slipmatt"]
    assert releases[0]["slug"] == "dreamscape-7-back-to-our-roots-the-sanctuary-26-11-93"


def test_ravearchive_release_extracts_the_mixcloud_player(monkeypatch):
    from core import ravearchive

    _route(monkeypatch, {
        ravearchive.FEED: RAVEARCHIVE_FEED,
        "https://rave-archive.com/dreamscape-7-back-to-our-roots-the-sanctuary-26-11-93/":
            RAVEARCHIVE_PAGE,
    })
    release = ravearchive.release("dreamscape-7-back-to-our-roots-the-sanctuary-26-11-93")
    assert release["mixcloud"] == ("https://www.mixcloud.com/Dizzyuk/"
                                   "ltj-bukem-dreamscape-7-the-sanctuary-261193/")


# --------------------------------------------------------------------------- GetDarker


def test_getdarker_extracts_mixcloud_from_the_feed(monkeypatch):
    from core import getdarker

    _route(monkeypatch, {getdarker.FEED: GETDARKER_FEED})
    sets = getdarker.sets()
    assert sets[0]["mixcloud"] == "https://www.mixcloud.com/getdarker/skream-benga-fwd-rinse-matter-20112009/"
    assert sets[1]["mixcloud"] == ""


def test_getdarker_falls_back_to_the_mixcloud_account(monkeypatch):
    from core import getdarker, mixcloud

    _route(monkeypatch, {})
    monkeypatch.setattr(mixcloud, "account", lambda user, limit=24: [{
        "title": "Skream - Watch The Ride [2008]",
        "url": "https://www.mixcloud.com/getdarker/skream-watch-the-ride-2008/",
        "created": "2008-01-01",
    }])
    sets = getdarker.sets()
    assert sets[0]["title"] == "Skream - Watch The Ride [2008]"
    assert sets[0]["mixcloud"].endswith("skream-watch-the-ride-2008/")


# --------------------------------------------------------------------------- curated data + routes


def test_pirate_radio_seed_data_has_the_exact_shared_schema():
    from core import pirate

    entries = pirate.entries()
    assert len(entries) == 5
    for entry in entries:
        assert set(entry) == set(pirate.FIELDS)
        assert entry["station"] and entry["title"] and entry["credit"]
        assert isinstance(entry["year"], str) and isinstance(entry["djs"], str)
    stations = {group["station"]: group for group in pirate.stations()}
    assert set(stations) == {"Risk FM 107.1", "Weekend Rush 92.3", "Flava FM"}
    assert stations["Risk FM 107.1"]["count"] == 2
    assert stations["Weekend Rush 92.3"]["years"] == ["1993", "1995"]
    flava = pirate.station("flava-fm")[0]
    assert flava["source_url"] == "" and flava["stream_url"] == ""
    assert "megaupload" in flava["credit"].lower()


def test_api_pirate_lists_stations_and_detail_entries(web):
    payload = web.get("/api/pirate").get_json()
    labels = [item["label"] for item in payload["items"]]
    assert labels == ["Risk FM 107.1", "Weekend Rush 92.3", "Flava FM"]
    assert payload["items"][1]["sub"].startswith("2 archives")

    risk = web.get("/api/pirate/risk-fm-107-1").get_json()
    assert risk["title"] == "Risk FM 107.1"
    playable = [item for item in risk["items"] if item.get("play")]
    assert playable and playable[0]["play"].startswith("https://")
    assert playable[0]["external"].startswith("https://www.thebeatsanctuary.co.uk/")

    flava = web.get("/api/pirate/flava-fm").get_json()
    assert "external" not in flava["items"][0]
    assert flava["items"][0]["credit"]
    assert web.get("/api/pirate/nope").status_code == 404


def test_api_moxser_list_and_stream_resolution(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.moxser, "mixes", lambda limit=48: [{
        "slug": "alpha-show-1062-26-october-2026",
        "title": "Alpha Show 1062 26 October 2026",
        "date": "26 October 2026",
        "flyer": "https://moxser.com/assets/uploads/primary/Alpha-Show.jpg",
    }])
    payload = web.get("/api/moxser").get_json()
    item = payload["items"][0]
    assert item["play_via"] == "/api/moxser/alpha-show-1062-26-october-2026"
    assert item["art"].endswith("Alpha-Show.jpg")

    monkeypatch.setattr(app_module.moxser, "detail", lambda slug: {
        "title": "Alpha Show 1062 (26 October 2026)",
        "date": "26 October 2026",
        "url": "https://enceladus.feralhosting.com/moxser/2026/90104/Alpha%20Show.mp3",
        "flyer": "",
    })
    resolved = web.get(item["play_via"]).get_json()
    assert resolved["url"].startswith("https://enceladus.feralhosting.com/moxser/")

    monkeypatch.setattr(app_module.moxser, "detail", lambda slug: None)
    assert web.get("/api/moxser/nope").status_code == 404


def test_api_hardscore_audio_rows_and_event_links(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.hardscore, "mixes", lambda limit=30: [
        {"title": "Force of One Crew - Weekend Rush", "date": "2023-11-22",
         "link": "https://www.hardscore.com/?p=11680",
         "media": "https://hardscore.com/mixes/WR/force.mp3"},
        {"title": "No audio post", "date": "2009-10-08",
         "link": "https://www.hardscore.com/?p=1250", "media": ""},
    ])
    monkeypatch.setattr(app_module.hardscore, "events", lambda limit=10: [
        {"title": "Oldskool Crew Live", "date": "2011-12-15",
         "link": "https://www.hardscore.com/?p=2238", "media": ""},
    ])
    payload = web.get("/api/hardscore").get_json()
    titles = [item["label"] for item in payload["items"]]
    assert titles == ["Force of One Crew - Weekend Rush", "No audio post", "Oldskool Crew Live"]
    assert payload["items"][0]["play"].endswith("force.mp3")
    assert payload["items"][1]["external"] == "https://www.hardscore.com/?p=1250"
    assert payload["items"][2]["external"] == "https://www.hardscore.com/?p=2238"


def test_api_ravearchive_list_and_mixcloud_detail(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.ravearchive, "releases", lambda limit=30: [{
        "title": "Dreamscape 7", "slug": "dreamscape-7", "link": "https://rave-archive.com/d/",
        "year": "1993", "djs": ["Dreamscape", "DJ Slipmatt"], "date": "2024-11-21",
    }])
    payload = web.get("/api/ravearchive").get_json()
    item = payload["items"][0]
    assert item["label"] == "Dreamscape 7"
    assert item["sub"] == "1993 · Dreamscape, DJ Slipmatt"
    monkeypatch.setattr(app_module.ravearchive, "release", lambda slug: {
        "title": "Dreamscape 7", "slug": slug, "link": "https://rave-archive.com/d/",
        "mixcloud": "https://www.mixcloud.com/Dizzyuk/dreamscape-7/",
    })
    detail = web.get(item["open"]).get_json()
    assert detail["items"][0]["embed"].startswith("https://www.mixcloud.com/widget/iframe/")
    assert detail["items"][0]["external"].endswith("dreamscape-7/")


def test_api_getdarker_embeds_cloudcasts(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.getdarker, "sets", lambda limit=30: [
        {"title": "Skream & Benga - FWD & Rinse", "date": "2024-11-20",
         "link": "https://getdarker.com/audio/sets/skream-benga/",
         "mixcloud": "https://www.mixcloud.com/getdarker/skream-benga/", "media": ""},
        {"title": "YouTube-only set", "date": "2025-07-05",
         "link": "https://getdarker.com/audio/sets/youtube/", "mixcloud": "", "media": ""},
    ])
    payload = web.get("/api/getdarker").get_json()
    assert payload["items"][0]["embed"].startswith("https://www.mixcloud.com/widget/iframe/")
    assert payload["items"][1]["external"] == "https://getdarker.com/audio/sets/youtube/"


def test_menu_lists_the_new_sections(web):
    payload = web.get("/api/menu").get_json()
    labels = {section["id"]: section["label"] for section in payload["sections"]}
    assert labels["moxser"] == "Moxser Mixes"
    assert labels["hardscore"] == "Hardscore"
    assert labels["ravearchive"] == "Rave Archive UK"
    assert labels["getdarker"] == "GetDarker"
    assert labels["pirates"] == "Pirate Radio Archives"


# --------------------------------------------------------------------------- stream proxy


def test_moxser_mp3_host_is_a_valid_stream_target(dns):
    """The proxy may fetch the Moxser MP3 host; the SSRF guard is unchanged."""
    import app as app_module

    dns["addresses"] = ["93.184.216.34"]
    scheme, host, port = app_module._parse_target(
        "https://enceladus.feralhosting.com/moxser/2026/90104/x.mp3")
    assert (scheme, host, port) == ("https", "enceladus.feralhosting.com", 443)
    # And a loopback redirect/lookup on that host is still refused.
    dns["addresses"] = ["127.0.0.1"]
    with pytest.raises(app_module.BlockedURL):
        app_module._resolve_target("https://enceladus.feralhosting.com/moxser/x.mp3")
