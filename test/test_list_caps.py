# -*- coding: utf-8 -*-
"""1.6.42 — no hidden list caps: default routes return the upstream list."""

import app as app_module
from core import feeds, getdarker, hardscore, moxser, ravearchive, toronto


def _route(monkeypatch, module, mapping):
    monkeypatch.setattr(module.fetch, "fetch",
                        lambda url, timeout=None, headers=None, ttl=0: mapping.get(url))
    if hasattr(module, "_throttle"):
        monkeypatch.setattr(module, "_throttle", lambda: None)


def _walk(web, url):
    """Follow the pager's next links and return every item exactly once."""
    items, page = [], 1
    while True:
        separator = "&" if "?" in url else "?"
        payload = web.get("%s%spage=%d" % (url, separator, page)).get_json()
        items.extend(payload["items"])
        if not (payload.get("pager") or {}).get("next"):
            return items
        page += 1


def _rss(title, count, body):
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>%s</title>%s'
            '</channel></rss>' % (title, body))


def test_moxser_default_is_the_complete_sitemap_list(monkeypatch):
    index = ['<?xml version="1.0"?><sitemapindex>']
    pages = {}
    entries = 0
    for page in range(1, 4):
        urls = []
        for index2 in range(25):
            entries += 1
            url = 'https://moxser.com/mixes/mix-%03d-01-january-2026' % entries
            urls.append('<url><loc>%s</loc><lastmod>2026-01-01T00:00:00+00:00</lastmod></url>' % url)
        page_url = 'https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p%d.xml' % page
        index.append('<sitemap><loc>%s</loc></sitemap>' % page_url)
        pages[page_url] = ('<?xml version="1.0"?><urlset>%s</urlset>' % ''.join(urls))
    index_url = 'https://moxser.com/sitemaps-1-sitemap.xml'
    _route(monkeypatch, moxser, dict(pages, **{index_url: ''.join(index) + '</sitemapindex>'}))
    rows = moxser.mixes()
    assert len(rows) == entries == 75
    # The old 48/200 caps are gone; an explicit internal limit still works.
    assert len(moxser.mixes(limit=10)) == 10


def test_hardscore_default_returns_all_mixes_and_events(monkeypatch):
    mix_items = ''.join(
        '<item><title>Mix %d</title><link>https://www.hardscore.com/?p=%d</link>'
        '<pubDate>Wed, 22 Nov 2023 10:00:00 +0000</pubDate>'
        '<content:encoded><![CDATA[<a href="https://hardscore.com/mixes/%d.mp3">x</a>]]>'
        '</content:encoded></item>' % (i, i, i) for i in range(45))
    event_items = ''.join(
        '<item><title>Event %d</title><link>https://www.hardscore.com/?p=%d</link>'
        '<pubDate>Wed, 22 Nov 2023 10:00:00 +0000</pubDate></item>' % (i, 1000 + i)
        for i in range(25))
    mix_feed = ('<?xml version="1.0"?><rss version="2.0" '
                'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                '<channel><title>Mixes</title>%s</channel></rss>' % mix_items)
    event_feed = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                  '<title>Events</title>%s</channel></rss>' % event_items)
    _route(monkeypatch, hardscore, {hardscore.FEED: mix_feed,
                                    hardscore.EVENTS_FEED: event_feed})
    assert len(hardscore.mixes()) == 45
    assert len(hardscore.events()) == 25


def test_podcast_episodes_are_not_capped_at_60(web, monkeypatch):
    items = ''.join(
        '<item><title>Episode %d</title><enclosure url="https://cdn/%d.mp3" '
        'type="audio/mpeg"/><pubDate>Wed, 22 Nov 2023 10:00:00 +0000</pubDate>'
        '</item>' % (i, i) for i in range(70))
    document = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                '<title>Feed</title>%s</channel></rss>' % items)
    monkeypatch.setattr(feeds, "registry", lambda: [
        {"id": "f", "name": "Feed", "url": "https://example.com/feed", "plot": ""}])
    monkeypatch.setattr(feeds, "feed", lambda feed_id: {
        "id": "f", "name": "Feed", "url": "https://example.com/feed", "plot": ""})
    monkeypatch.setattr(feeds.http, "fetch",
                        lambda url, timeout=None, headers=None, ttl=0: document)
    assert len(feeds.episodes("f")) == 70
    payload = web.get("/api/podcast/f").get_json()
    assert len(payload["items"]) == 40
    assert payload["pager"]["total"] == 70
    assert len(_walk(web, "/api/podcast/f")) == 70


def test_ravearchive_and_getdarker_defaults_are_complete(monkeypatch):
    releases = ''.join(
        '<item><title>Release %d</title><link>https://rave-archive.com/release-%d/</link>'
        '<pubDate>Wed, 22 Nov 2023 10:00:00 +0000</pubDate>'
        '<category>1993</category></item>' % (i, i) for i in range(45))
    release_feed = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                    '<title>RA</title>%s</channel></rss>' % releases)
    _route(monkeypatch, ravearchive, {ravearchive.FEED: release_feed})
    assert len(ravearchive.releases()) == 45

    sets = ''.join(
        '<item><title>Set %d</title><link>https://getdarker.com/set-%d/</link>'
        '<pubDate>Wed, 22 Nov 2023 10:00:00 +0000</pubDate>'
        '<content:encoded><![CDATA[<iframe src="https://www.mixcloud.com/widget/'
        'iframe/?feed=https%%3A%%2F%%2Fwww.mixcloud.com%%2Fgetdarker%%2Fset-%d%%2F">'
        '</iframe>]]></content:encoded></item>' % (i, i, i) for i in range(45))
    set_feed = ('<?xml version="1.0"?><rss version="2.0" '
                'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                '<channel><title>GD</title>%s</channel></rss>' % sets)
    _route(monkeypatch, getdarker, {getdarker.FEED: set_feed})
    assert len(getdarker.sets()) == 45


def test_toronto_genre_route_returns_all_rows(web, monkeypatch):
    rows = [{"title": "Toronto mix %d" % i,
             "url": "https://torontoravemixtapearchive.com/files/mixtape/%d.mp3" % i,
             "date": "1993-09-04", "venue": "Syrous", "duration": "40:00"}
            for i in range(1001)]
    monkeypatch.setattr(toronto, "_genre_rows", lambda genre: rows)
    payload = web.get("/api/toronto?genre=jungle").get_json()
    assert len(payload["items"]) == 40
    assert payload["pager"]["pages"] == 26
    assert len(_walk(web, "/api/toronto?genre=jungle")) == 1001


def test_source_routes_render_every_row(web, monkeypatch):
    mox = [{"title": "Mox %d" % i, "slug": "mox-%d" % i, "date": "2026-01-01",
            "flyer": ""} for i in range(60)]
    monkeypatch.setattr(app_module.moxser, "mixes", lambda: mox)
    assert len(web.get("/api/moxser").get_json()["items"]) == 40
    assert len(_walk(web, "/api/moxser")) == 60

    mixes = [{"title": "Mix %d" % i, "date": "2023-11-22",
              "media": "https://hardscore.com/mixes/%d.mp3" % i, "link": ""}
             for i in range(45)]
    events = [{"title": "Event %d" % i, "date": "2023-11-22",
               "link": "https://www.hardscore.com/?p=%d" % i, "media": ""}
              for i in range(25)]
    monkeypatch.setattr(app_module.hardscore, "mixes", lambda: mixes)
    monkeypatch.setattr(app_module.hardscore, "events", lambda: events)
    assert len(web.get("/api/hardscore").get_json()["items"]) == 40
    assert len(_walk(web, "/api/hardscore")) == 70

    releases = [{"title": "Release %d" % i, "slug": "release-%d" % i,
                 "year": "1993", "djs": ["DJ"]} for i in range(45)]
    monkeypatch.setattr(app_module.ravearchive, "releases", lambda: releases)
    assert len(web.get("/api/ravearchive").get_json()["items"]) == 40
    assert len(_walk(web, "/api/ravearchive")) == 45

    sets = [{"title": "Set %d" % i, "date": "2023-11-22",
             "mixcloud": "https://www.mixcloud.com/getdarker/set-%d/" % i}
            for i in range(45)]
    monkeypatch.setattr(app_module.getdarker, "sets", lambda: sets)
    assert len(web.get("/api/getdarker").get_json()["items"]) == 40
    assert len(_walk(web, "/api/getdarker")) == 45
