# -*- coding: utf-8 -*-
"""Deterministic API/rail benchmark for the web build (1.6.18 perf pass).

Runs the real Flask views with the real disk/in-process caches but a fake
network (core.fetch._request), so cold vs warm and payload changes are
repeatable on a laptop.  Usage:

    .venv/bin/python scripts/bench_api.py [--samples 25] [--latency-ms 20]

Prints a markdown table and writes ``bench-api.json`` next to the cwd.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = tempfile.mkdtemp(prefix="raveweb-bench-")
os.environ["RAVEWEB_DATA"] = BASE
os.environ["RAVEPLAYER_CACHE_DIR"] = os.path.join(BASE, "cache")
os.environ["RAVEPLAYER_FAV_DIR"] = os.path.join(BASE, "favs")
os.environ.setdefault("SECRET_KEY", "bench-secret-key")
os.environ.setdefault("RAVEWEB_PASSWORD", "bench-password")

import app as app_module  # noqa: E402
from core import cache, downloads, fetch, moxser  # noqa: E402

RESULTS = []
LATENCY = 0.02


def _search_json():
    docs = [{"identifier": "pack-%d" % i, "title": "Bench Pack %d" % i,
             "year": str(1988 + (i % 30)), "downloads": 1000 - i}
            for i in range(60)]
    return json.dumps({"response": {"numFound": 500, "start": 0, "docs": docs}})


def _metadata_json(identifier):
    files = []
    for i in range(20):
        files.append({"name": "Tape %02d/%02d - Track.mp3" % (i // 10 + 1, i + 1),
                      "source": "original", "format": "MP3", "size": "1024",
                      "length": "3:07", "title": "Bench Track %d" % i})
    return json.dumps({
        "metadata": {"title": "Bench Pack", "year": "1993"},
        "files": files, "dir": "/0/items/%s" % identifier,
        "d1": "ia801504.us.archive.org", "server": "ia801504.us.archive.org",
    })


def _feed_xml():
    items = "".join(
        "<item><title>Bench Show %d</title><link>https://example.com/%d</link>"
        "<pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>"
        "<enclosure url=\"https://cdn.example/%d.mp3\" type=\"audio/mpeg\"/>"
        "<itunes:duration>3600</itunes:duration></item>" % (i, i, i)
        for i in range(20))
    return ('<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
            "<channel><title>Bench Feed</title>%s</channel></rss>" % items)


def _dnb_html():
    return "".join('<a href="/listen?mix=%d" title="2026-01-01 : Bench Show %d">x</a>' % (i, i)
                   for i in range(10))


def _moxser_index():
    return ('<?xml version="1.0"?><sitemapindex>'
            '<sitemap><loc>https://moxser.com/sitemaps-1-section-artist-1-sitemap-p1.xml</loc></sitemap>'
            '<sitemap><loc>https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p1.xml</loc></sitemap>'
            '<sitemap><loc>https://moxser.com/sitemaps-1-section-mixes-1-sitemap-p2.xml</loc></sitemap>'
            "</sitemapindex>")


def _moxser_sitemap(start, count):
    parts = []
    for i in range(start, start + count):
        slug = "bench-mix-%d-01-january-2026" % i
        parts.append(
            "<url><loc>https://moxser.com/mixes/%s</loc>"
            "<lastmod>2026-01-%02dT00:00:00+00:00</lastmod>"
            "<image:image><image:loc>https://moxser.com/assets/uploads/primary/%d.jpg</image:loc>"
            "<image:title>Bench+Mix+%d</image:title></image:image></url>" % (slug, (i % 28) + 1, i, i))
    return '<?xml version="1.0"?><urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">%s</urlset>' % "".join(parts)


def _moxser_page(slug):
    return ('<html><h1 class="font-display">Bench Mix (01 January 2026)</h1>'
            "<script>window.x = { path: 'https://enceladus.feralhosting.com/moxser/2026/1/%s.mp3' };</script></html>" % slug)


def fake_request(url, timeout=None, headers=None):
    time.sleep(LATENCY)
    if "advancedsearch" in url:
        return _search_json()
    if "metadata/" in url:
        return _metadata_json(url.rsplit("/", 1)[-1].split("?")[0])
    if "dnbradio" in url:
        return _dnb_html()
    if "sitemaps-1-sitemap.xml" in url:
        return _moxser_index()
    if "section-mixes-1-sitemap-p1" in url:
        return _moxser_sitemap(0, 60)
    if "section-mixes-1-sitemap-p2" in url:
        return _moxser_sitemap(60, 5)
    if "moxser.com/mixes/" in url:
        return _moxser_page(url.rsplit("/", 1)[-1])
    if "advancedsearch" in url or "archive.org" in url:
        return _search_json()
    return _feed_xml()


def seed_downloads():
    folder = downloads.directory(BASE)
    entries = []
    now = time.time()
    for i in range(20):
        name = "Track-%02d-aaaaaaaa.mp3" % i
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(b"ID3" + b"x" * 2048)
        entries.append({
            "id": "aaaaaaaa%08d" % i, "title": "Downloaded Track %d" % i,
            "filename": name, "source": "archive",
            "url": "https://archive.org/download/pack/track-%d.mp3" % i,
            "size": 2051, "created": now - i,
        })
    with open(downloads.index_path(BASE), "w", encoding="utf-8") as handle:
        json.dump(entries, handle)


def reset_caches():
    shutil.rmtree(os.path.join(BASE, "cache"), ignore_errors=True)
    for module in (cache, moxser, app_module):
        reset = getattr(module, "clear_memory", None)
        if reset:
            try:
                reset()
            except TypeError:
                pass


def measure(client, path, headers=None):
    started = time.perf_counter()
    response = client.get(path, headers=headers or {})
    elapsed = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200, (path, response.status_code, response.data[:200])
    return elapsed, len(response.data)


def percentile(values, pct):
    values = sorted(values)
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round((pct / 100.0) * (len(values) - 1)))))
    return values[index]


def bench(path, samples):
    client = app_module.app.test_client()
    reset_caches()
    cold_ms, cold_bytes = measure(client, path)
    warm = [measure(client, path)[0] for _ in range(samples)]
    plain_json = measure(client, path)[1]
    gzipped = measure(client, path, headers={"Accept-Encoding": "gzip"})[1]
    return {
        "path": path,
        "cold_ms": round(cold_ms, 1),
        "warm_p50_ms": round(percentile(warm, 50), 1),
        "warm_p95_ms": round(percentile(warm, 95), 1),
        "bytes": plain_json,
        "bytes_gzip": gzipped,
    }


def main():
    global LATENCY
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--latency-ms", type=float, default=20.0)
    args = parser.parse_args()
    LATENCY = args.latency_ms / 1000.0
    fetch._request = fake_request
    seed_downloads()
    for path in ("/api/latest", "/api/filters", "/api/item/pack-0",
                 "/api/moxser", "/api/downloads"):
        RESULTS.append(bench(path, args.samples))
    print("| Endpoint | Cold ms | Warm p50 ms | Warm p95 ms | Bytes | Bytes (gzip) |")
    print("|---|---|---|---|---|---|")
    for row in RESULTS:
        print("| `%s` | %.1f | %.1f | %.1f | %d | %d |" % (
            row["path"], row["cold_ms"], row["warm_p50_ms"], row["warm_p95_ms"],
            row["bytes"], row["bytes_gzip"]))
    with open(os.path.join(os.getcwd(), "bench-api.json"), "w", encoding="utf-8") as handle:
        json.dump({"latency_ms": args.latency_ms, "samples": args.samples,
                   "results": RESULTS}, handle, indent=2)


if __name__ == "__main__":
    main()
