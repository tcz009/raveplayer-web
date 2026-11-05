# -*- coding: utf-8 -*-
"""Hermetic pagination benchmark for the web build.

Every provider is replaced with a deterministic in-memory list before the app
is imported/used, so the numbers measure *our* paging/serialisation cost and
never the Internet.  Cold = caches cleared, warm = the next five requests.

    .venv/bin/python scripts/bench_pagination.py --samples 5 [--json out.json]
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = tempfile.mkdtemp(prefix="raveweb-page-bench-")
os.environ["RAVEWEB_DATA"] = BASE
os.environ["RAVEPLAYER_CACHE_DIR"] = os.path.join(BASE, "cache")
os.environ["RAVEPLAYER_FAV_DIR"] = os.path.join(BASE, "favs")
os.environ.setdefault("SECRET_KEY", "bench-secret-key")
os.environ.setdefault("RAVEWEB_PASSWORD", "bench-password")
os.environ.setdefault("RAVEWEB_ENV", "test")

import app as app_module  # noqa: E402
from core import audit, cache, moxser  # noqa: E402

ROWS = app_module.ROWS


def toronto_rows(count):
    return [{"title": "Toronto mix %04d" % index,
             "url": "https://toronto.example/files/%d.mp3" % index,
             "date": "1993-09-04", "venue": "Syrous", "duration": "40:00"}
            for index in range(count)]


def moxser_rows(count):
    return [{"title": "Mox mix %04d" % index, "slug": "mox-%04d" % index,
             "date": "2026-01-01", "flyer": ""} for index in range(count)]


def jt_rows(count):
    return [{"key": "show-%04d" % index, "name": "Show %04d" % index}
            for index in range(count)]


def rave_rows(count):
    return [{"name": "Rave %03d" % index, "slug": "rave-%03d" % index,
             "archive_count": index, "mixcloud_count": 0}
            for index in range(count)]


def archive_docs(count):
    return [{"identifier": "pack-%04d" % index, "title": "Pack %04d" % index,
             "year": "1994"} for index in range(count)]


def archive_tracks(count):
    return [{"title": "Track %04d" % index,
             "url": "https://archive.org/download/p/%d.mp3" % index,
             "length": "180"} for index in range(count)]


def install_fixtures():
    app_module.toronto.genre_items = lambda genre, limit=None: toronto_rows(2640)
    app_module.toronto.search = lambda query, limit=None: toronto_rows(679)
    moxser.mixes = lambda: moxser_rows(1001)
    app_module.legacy.jungle_shows = lambda: jt_rows(310)
    app_module.raves.registry = lambda: rave_rows(55)
    docs = archive_docs(2400)

    def fake_search(query, page=1, rows=50, sort="downloads desc"):
        start = (page - 1) * rows
        return docs[start:start + rows], len(docs)

    app_module.archive.search = fake_search
    app_module.archive.audio_files = lambda identifier: archive_tracks(1645)
    app_module.archive.thumb = lambda identifier: "/static/art/tapepacks.png"


def login_client():
    app_module.app.config.update(TESTING=True)
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


def reset_caches():
    for module in (cache, moxser):
        clear = getattr(module, "clear_memory", None)
        if clear:
            clear()


def measure(client, path):
    started = time.perf_counter()
    response = client.get(path)
    elapsed = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200, (path, response.status_code)
    body = response.get_json()
    pager = body.get("pager") or {}
    return elapsed, len(response.data), len(body.get("items") or []), pager


def bench(client, path, samples):
    reset_caches()
    cold_ms, cold_bytes, cold_items, pager = measure(client, path)
    warm = [measure(client, path)[0] for _ in range(samples)]
    return {
        "path": path,
        "page": pager.get("page"),
        "pages": pager.get("pages"),
        "items": cold_items,
        "total": pager.get("total"),
        "cold_ms": round(cold_ms, 1),
        "warm_p50_ms": round(statistics.median(warm), 1),
        "warm_p95_ms": round(sorted(warm)[max(0, int(len(warm) * 0.95) - 1)], 1),
        "bytes": cold_bytes,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    install_fixtures()
    client = login_client()
    cases = [
        ("/api/toronto?genre=jungle", "Toronto 2,640 · page 1"),
        ("/api/toronto?genre=jungle&page=66", "Toronto 2,640 · last"),
        ("/api/moxser", "Moxser 1,001 · page 1"),
        ("/api/moxser?page=26", "Moxser 1,001 · last"),
        ("/api/legacy/jungletapes", "JungleTapes 310 · page 1"),
        ("/api/legacy/jungletapes?page=8", "JungleTapes 310 · last"),
        ("/api/raves", "Rave Nights 55 · page 1"),
        ("/api/raves?page=2", "Rave Nights 55 · last"),
        ("/api/item/helter-skelter-tape-packs", "Pack 1,645 · page 1"),
        ("/api/item/helter-skelter-tape-packs?page=42", "Pack 1,645 · last"),
        ("/api/search?q=pack", "Search 2,400 · page 1"),
        ("/api/search?q=pack&page=60", "Search 2,400 · last"),
    ]
    rows = [bench(client, path, args.samples) for path, _label in cases]
    print("| List | Page | Pages | Items | Cold ms | Warm p50 ms | Warm p95 ms | Bytes |")
    print("|---|---|---|---|---|---|---|---|")
    for (path, label), row in zip(cases, rows):
        print("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            label, row["page"], row["pages"], row["items"],
            row["cold_ms"], row["warm_p50_ms"], row["warm_p95_ms"], row["bytes"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
