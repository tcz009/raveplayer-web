# -*- coding: utf-8 -*-
"""Deterministic API benchmark for the web build.

Runs the real Flask views with the real disk/in-process caches but a fake
network (``core.fetch._request``), so cold vs warm and payload changes are
repeatable on a laptop.  Authenticates like a real owner session (1.6.21+), and
replays the archived RaveTapePacks pages from ``test/fixtures/bench`` so the
RTP routes are measurable even while the site blocks this network.

    .venv/bin/python scripts/bench_api.py [--samples 25] [--latency-ms 20]
    .venv/bin/python scripts/bench_api.py --only /api/latest,/api/menu

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
from core import audit, cache, downloads, fetch, moxser  # noqa: E402

RESULTS = []
LATENCY = 0.02
FIXTURES = os.path.join(ROOT, "test", "fixtures", "bench")
RTP_ARCHIVE_MP3 = ("https://www.ravetapepacks.com/music/BenchPack/"
                   "Bench_Track.mp3")


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


def _fixture(name):
    try:
        with open(os.path.join(FIXTURES, name), encoding="utf-8",
                  errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _rtp_page(url):
    """Archived RTP page for a replay URL, or None for other hosts."""
    try:
        from urllib.parse import parse_qs, urlsplit
        split = urlsplit(url)
    except Exception:  # noqa: BLE001
        return None
    if (split.hostname or "").lower() not in ("ravetapepacks.com",
                                              "www.ravetapepacks.com"):
        return None
    if split.path not in ("", "/"):
        return None
    if not split.query:
        return _fixture("rtp-home.html")
    query = parse_qs(split.query)
    if (query.get("cat") or [""])[0].isdigit():
        return _fixture("rtp-cat-331.html")
    if (query.get("p") or [""])[0].isdigit():
        # No standalone post capture: the archived category page is a real
        # 155 KB RTP body with 48 MP3 links (see fixtures/PROVENANCE.md).
        return _fixture("rtp-cat-331.html")
    return None


def fake_request(url, timeout=None, headers=None):
    page = _rtp_page(url)
    if page is not None:
        time.sleep(LATENCY)
        return page
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


def login_client():
    """A real owner session (the API has required auth since 1.6.21)."""
    client = app_module.app.test_client()
    sid = audit.create_session("owner", "admin", "127.0.0.1", "bench", 3600)
    with client.session_transaction() as session:
        session["auth"] = True
        session["identity"] = "owner"
        session["user"] = "owner"
        session["role"] = "admin"
        session["sid"] = sid
        session["login_at"] = time.time()
        session["remember"] = False
        session["csrf"] = "bench-csrf"
    return client


class _FakeConn(object):
    def close(self):
        pass


class _FakeResponse(object):
    """Minimal upstream response for the /api/stream route."""

    def __init__(self, body=b"ID3" + b"x" * 65536, status=200):
        self.status = status
        self._body = body
        self._offset = 0

    def getheader(self, name, default=None):
        return {
            "Content-Type": "audio/mpeg",
            "Accept-Ranges": "bytes",
        }.get(name, default)

    def read1(self, size):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass


def fake_open_stream(url, headers, deadline):
    return _FakeConn(), _FakeResponse(), url


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


def bench(path, samples, client=None):
    client = client or login_client()
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


def bench_stream(samples):
    """Play-start: server-side time to the first byte of ``/api/stream``.

    ``_open_stream`` is replaced with an in-memory upstream, so this measures
    the proxy's own cost (auth, SSRF checks, headers, first-byte relay) rather
    than the provider's network latency.
    """
    client = login_client()
    import urllib.parse as _parse
    path = "/api/stream?u=" + _parse.quote(RTP_ARCHIVE_MP3, safe="")
    original = app_module._open_stream
    app_module._open_stream = fake_open_stream
    try:
        reset_caches()
        started = time.perf_counter()
        response = client.get(path)
        elapsed = (time.perf_counter() - started) * 1000.0
        assert response.status_code == 200, (response.status_code, response.data[:200])
        response.close()
        warm = []
        for _ in range(max(1, samples)):
            started = time.perf_counter()
            response = client.get(path)
            warm.append((time.perf_counter() - started) * 1000.0)
            assert response.status_code == 200, response.status_code
            response.close()
    finally:
        app_module._open_stream = original
    return {
        "path": "/api/stream (first byte)",
        "cold_ms": round(elapsed, 1),
        "warm_p50_ms": round(percentile(warm, 50), 1),
        "warm_p95_ms": round(percentile(warm, 95), 1),
        "bytes": 0,
        "bytes_gzip": 0,
    }


DEFAULT_PATHS = (
    "/api/menu",
    "/api/rail/packs",
    "/api/latest",
    "/api/filters",
    "/api/item/pack-0",
    "/api/moxser",
    "/api/moxser/bench-mix-1-01-january-2026",
    "/api/downloads",
    "/api/search?q=dreamscape",
    "/api/legacy/ravetapepacks",
    "/api/legacy/ravetapepacks/all",
    "/api/legacy/ravetapepacks/index/events",
    "/api/legacy/ravetapepacks/index/events/331",
    "/api/legacy/ravetapepacks/index/djs/106",
    "/api/legacy/ravetapepacks/post/899",
    "/api/legacy/jungletapes",
    "/api/toronto",
)


def main():
    global LATENCY
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--latency-ms", type=float, default=20.0)
    parser.add_argument("--only", default="")
    parser.add_argument("--json", default="")
    parser.add_argument("--skip-stream", action="store_true")
    args = parser.parse_args()
    LATENCY = args.latency_ms / 1000.0
    fetch._request = fake_request
    seed_downloads()
    paths = [part.strip() for part in args.only.split(",") if part.strip()] \
        or list(DEFAULT_PATHS)
    client = login_client()
    for path in paths:
        RESULTS.append(bench(path, args.samples, client=client))
    if not args.skip_stream:
        RESULTS.append(bench_stream(args.samples))
    print("| Endpoint | Cold ms | Warm p50 ms | Warm p95 ms | Bytes | Bytes (gzip) |")
    print("|---|---|---|---|---|---|")
    for row in RESULTS:
        print("| `%s` | %.1f | %.1f | %.1f | %d | %d |" % (
            row["path"], row["cold_ms"], row["warm_p50_ms"], row["warm_p95_ms"],
            row["bytes"], row["bytes_gzip"]))
    target = args.json or os.path.join(os.getcwd(), "bench-api.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump({"latency_ms": args.latency_ms, "samples": args.samples,
                   "results": RESULTS}, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
