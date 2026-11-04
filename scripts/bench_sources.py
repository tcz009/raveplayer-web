#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Timing harness for the legacy/curated sources, on web and Kodi.

Reports, per source and stage, the wall-clock split between **upstream fetch**,
**cache read** and **parse/serialise** (everything that is not HTTP), plus the
row/item count and web payload size.  Cold samples clear both the in-process and
the on-disk cache; warm samples reuse it.  p50/p95 are computed per stage.

The script detects the platform the same way ``audit_legacy_links.py`` does:

    cd "Rave Player" && ./.venv/bin/python scripts/bench_sources.py
    cd raveplayer && python3 scripts/bench_sources.py

RaveTapePacks currently blocks this machine's IP at the TCP level.  ``--replay
auto`` (the default) therefore serves the two archived RTP pages from
``test/fixtures/bench`` with a simulated upstream latency, while every other
source is measured live.  Use ``--replay none`` to force live RTP (it will time
out here) or ``--replay all`` for a fully offline run.

    # Honest before/after comparison: same replay bytes, same simulated latency
    python3 scripts/bench_sources.py --cold-samples 3 --samples 5 \
        --json bench-before.json

The harness never writes to the repo cache: it uses a throwaway
``RAVEPLAYER_CACHE_DIR`` unless ``--cache-dir`` is given.
"""

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.isdir(os.path.join(ROOT, "core"))

# The cache/env paths are read at call time, but set them before importing
# anything that might touch them at import time.
_BENCH_CACHE = tempfile.mkdtemp(prefix="rave-bench-cache-")
os.environ["RAVEPLAYER_CACHE_DIR"] = _BENCH_CACHE
_BENCH_DATA = tempfile.mkdtemp(prefix="rave-bench-data-")
if WEB:
    os.environ["RAVEWEB_DATA"] = _BENCH_DATA
    os.environ.setdefault("SECRET_KEY", "bench-secret-key")
    os.environ.setdefault("RAVEWEB_PASSWORD", "bench-password")

if WEB:
    sys.path.insert(0, ROOT)
    from core import archive, cache, hardscore, legacy, moxser  # noqa: E402
    from core import fetch as http_mod  # noqa: E402
    from core import toronto  # noqa: E402
else:
    sys.path.insert(0, os.path.join(ROOT, "test", "stubs"))
    sys.path.insert(0, ROOT)
    from resources.lib import archive, cache, hardscore, legacy, moxser  # noqa: E402
    from resources.lib import http as http_mod  # noqa: E402
    from resources.lib import toronto  # noqa: E402

FIXTURES = os.path.join(ROOT, "test", "fixtures", "bench")
PLATFORM = "web" if WEB else "kodi"
RTP_HOSTS = ("ravetapepacks.com", "www.ravetapepacks.com")


# --------------------------------------------------------------------------- util


def percentile(values, pct):
    values = sorted(value for value in values if value is not None)
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    position = (len(values) - 1) * pct / 100.0
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def _clear_caches():
    reset = getattr(cache, "clear_memory", None)
    if reset:
        reset()
    shutil.rmtree(_BENCH_CACHE, ignore_errors=True)
    for module in (legacy, toronto, moxser):
        reset = getattr(module, "clear_memory", None)
        if reset:
            try:
                reset()
            except TypeError:
                pass


class Meter(object):
    """Wrap ``http.fetch`` and split time into upstream, cache and parse."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.upstream_ms = 0.0
        self.cache_ms = 0.0
        self.upstream_calls = 0
        self.cache_calls = 0
        self.urls = []

    def install(self):
        original = http_mod.fetch

        def timed(url, timeout=20, headers=None, ttl=0):
            hit = False
            if ttl:
                try:
                    key = http_mod._cache_key("text", url, headers)
                    hit = cache.get(key, ttl) is not cache.MISSING
                except Exception:  # noqa: BLE001 - classification is best-effort
                    hit = False
            started = time.perf_counter()
            body = original(url, timeout=timeout, headers=headers, ttl=ttl)
            elapsed = (time.perf_counter() - started) * 1000.0
            if hit:
                self.cache_ms += elapsed
                self.cache_calls += 1
            else:
                self.upstream_ms += elapsed
                self.upstream_calls += 1
            self.urls.append((url, "cache" if hit else "upstream", elapsed))
            return body

        http_mod.fetch = timed
        self._original = original

    def uninstall(self):
        http_mod.fetch = self._original


# --------------------------------------------------------------------------- replay


def _rtp_page(url):
    """Return archived RTP page text for ``url``, or None when not mapped."""
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (split.hostname or "").lower()
    if host not in RTP_HOSTS and not host.endswith(".ravetapepacks.com"):
        return None
    if split.path not in ("", "/"):
        return None
    if not split.query:
        name = "rtp-home.html"
    else:
        query = urllib.parse.parse_qs(split.query)
        cat_id = (query.get("cat") or [""])[0]
        if not cat_id.isdigit():
            return None
        name = "rtp-cat-%s.html" % cat_id
    path = os.path.join(FIXTURES, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def install_replay(mode, latency_ms):
    """Patch the HTTP request layer to serve archived RTP pages.

    ``auto`` replays RTP and leaves every other host live; ``all`` never touches
    the network; ``none`` is a no-op.  The patch is at ``_request`` so the real
    fetch/cache layer still runs and cache hits stay cache hits.
    """
    if mode == "none":
        return None
    original = http_mod._request
    latency = max(0.0, float(latency_ms)) / 1000.0

    def replay_request(url, timeout=None, headers=None):
        page = _rtp_page(url)
        if page is not None:
            if latency:
                time.sleep(latency)
            return page
        if mode == "all":
            return None
        return original(url, timeout, headers)

    http_mod._request = replay_request
    return original


# --------------------------------------------------------------------------- web


class WebHarness(object):
    def __init__(self):
        import app as app_module
        from core import audit

        self.app_module = app_module
        self.data_dir = _BENCH_DATA
        app_module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        audit.configure(os.path.join(self.data_dir, "auth.sqlite3"))
        self.client = app_module.app.test_client()
        sid = audit.create_session("owner", "admin", "127.0.0.1", "bench", 3600)
        with self.client.session_transaction() as session:
            session["auth"] = True
            session["identity"] = "owner"
            session["user"] = "owner"
            session["role"] = "admin"
            session["sid"] = sid
            session["login_at"] = time.time()
            session["remember"] = False
            session["csrf"] = "bench-csrf"

    def route(self, path):
        def run():
            response = self.client.get(path)
            try:
                payload = response.get_json(silent=True) or {}
            except Exception:  # noqa: BLE001
                payload = {}
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list):
                count = len(items)
            elif isinstance(payload, dict) and payload.get("url"):
                count = 1  # resolve endpoints return one media URL, not a list
            else:
                count = 0
            return {
                "status": response.status_code,
                "items": count,
                "bytes": len(response.data),
            }

        return run


# --------------------------------------------------------------------------- kodi


class KodiHarness(object):
    def __init__(self):
        import xbmcplugin  # test stub

        self.xbmcplugin = xbmcplugin
        from resources.lib import routes

        self.routes = routes

    def route(self, mode, **params):
        params = dict(params, mode=mode)
        query = "&".join(
            "%s=%s" % (key, urllib.parse.quote(str(value), safe=""))
            for key, value in params.items()
        )

        def run():
            self.xbmcplugin.ITEMS[:] = []
            self.xbmcplugin.RESOLVED[:] = []
            self.routes.dispatch(
                ["plugin://plugin.audio.raveplayer/", "1", "?" + query])
            return {"status": 200, "items": len(self.xbmcplugin.ITEMS), "bytes": 0}

        return run


# --------------------------------------------------------------------------- discovery


def _first_pack(entries):
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("key"):
            return entry
    return None


def _resolve_pack(key, kind=None, cat_id=None):
    """Resolve one RTP pack with the public API available on this tree."""
    finder = getattr(legacy, "pack", None)
    if callable(finder):
        try:
            return finder(key, kind=kind, cat_id=cat_id)
        except TypeError:
            return finder(key)
    for entry in legacy.packs():
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry
    if kind and cat_id:
        for entry in legacy.category_packs(kind, cat_id):
            if isinstance(entry, dict) and entry.get("key") == key:
                return entry
    return None


def discover():
    """Fetch the minimum identifiers the scenarios need (not timed)."""
    found = {}
    try:
        home_pack = _first_pack(legacy.packs())
    except Exception:  # noqa: BLE001
        home_pack = None
    found["rtp_pack_key"] = (home_pack or {}).get("key")
    found["rtp_pack_name"] = (home_pack or {}).get("name")

    found["rtp_cat_kind"] = "events"
    found["rtp_cat_id"] = None
    found["rtp_cat_pack_key"] = None
    try:
        categories = legacy.categories()
        events = categories.get("events") or []
        preferred = next((row for row in events if str(row.get("id")) == "331"), None)
        chosen = preferred or (events[0] if events else None)
        if chosen:
            found["rtp_cat_id"] = str(chosen["id"])
            category_pack = _first_pack(legacy.category_packs("events", chosen["id"]))
            found["rtp_cat_pack_key"] = (category_pack or {}).get("key")
    except Exception:  # noqa: BLE001
        pass

    try:
        shows = legacy.jungle_shows()
        found["jt_show"] = shows[0]["key"] if shows else None
    except Exception:  # noqa: BLE001
        found["jt_show"] = None

    if WEB:
        try:
            mixes = moxser.mixes(limit=1)
            found["moxser_slug"] = mixes[0].get("slug") if mixes else None
        except Exception:  # noqa: BLE001
            found["moxser_slug"] = None
    else:
        try:
            mixes = moxser.mixes(limit=1)
            found["moxser_page_url"] = mixes[0].get("page_url") if mixes else None
        except Exception:  # noqa: BLE001
            found["moxser_page_url"] = None

    try:
        docs, _total = archive.search(
            "collection:ravetapes AND mediatype:audio", rows=1)
        found["archive_id"] = docs[0].get("identifier") if docs else None
    except Exception:  # noqa: BLE001
        found["archive_id"] = None

    found["toronto_genre"] = "jungle"
    return found


# --------------------------------------------------------------------------- scenarios


def build_scenarios(harness, ids):
    """Return [(name, mode_label, callable), ...]."""
    scenarios = []

    def add(name, mode, run):
        if run is not None:
            scenarios.append((name, mode, run))

    if WEB:
        add("rtp_hub", "replay/live", harness.route("/api/legacy/ravetapepacks"))
        add("rtp_all", "replay/live", harness.route("/api/legacy/ravetapepacks/all"))
        add("rtp_index", "replay/live",
            harness.route("/api/legacy/ravetapepacks/index/djs"))
        if ids.get("rtp_cat_id"):
            add("rtp_category", "replay/live", harness.route(
                "/api/legacy/ravetapepacks/index/events/%s" % ids["rtp_cat_id"]))
        category_key = ids.get("rtp_cat_pack_key")
        if category_key:
            add("rtp_pack", "replay/live", harness.route(
                "/api/legacy/ravetapepacks/%s?cat=%s&kind=events"
                % (urllib.parse.quote(category_key),
                   urllib.parse.quote(ids["rtp_cat_id"]))))
        elif ids.get("rtp_pack_key"):
            add("rtp_pack", "replay/live", harness.route(
                "/api/legacy/ravetapepacks/%s"
                % urllib.parse.quote(ids["rtp_pack_key"])))
        add("jt_index", "live", harness.route("/api/legacy/jungletapes"))
        if ids.get("jt_show"):
            add("jt_show", "live", harness.route(
                "/api/legacy/jungletapes/%s" % urllib.parse.quote(ids["jt_show"])))
        add("toronto_index", "live", harness.route("/api/toronto"))
        add("toronto_genre", "live", harness.route("/api/toronto?genre=jungle"))
        add("moxser_index", "live", harness.route("/api/moxser"))
        if ids.get("moxser_slug"):
            add("moxser_detail", "live", harness.route(
                "/api/moxser/%s" % urllib.parse.quote(ids["moxser_slug"])))
        add("hardscore_index", "live", harness.route("/api/hardscore"))
        add("archive_list", "live", harness.route("/api/every-pack"))
        if ids.get("archive_id"):
            add("archive_item", "live", harness.route(
                "/api/item/%s" % urllib.parse.quote(ids["archive_id"])))
    else:
        add("rtp_index", "replay/live", harness.route("legacy_index", kind="event"))
        add("rtp_all", "replay/live", harness.route("legacy_packs"))
        if ids.get("rtp_pack_key"):
            add("rtp_pack", "replay/live", harness.route(
                "legacy_pack", pack=ids["rtp_pack_key"]))
        add("jt_index", "live", harness.route("legacy_jt"))
        if ids.get("jt_show"):
            add("jt_show", "live", harness.route("legacy_jt_show", key=ids["jt_show"]))
        add("toronto_index", "live", harness.route("toronto"))
        add("toronto_genre", "live", harness.route(
            "toronto_genre", genre="jungle"))
        add("moxser_index", "live", harness.route("moxser"))
        if ids.get("moxser_page_url"):
            add("moxser_detail", "live", harness.route(
                "moxser_mix", page=ids["moxser_page_url"]))
        add("hardscore_index", "live", harness.route("hardscore"))
        add("archive_list", "live", harness.route("archive_list", key="jungle"))
        if ids.get("archive_id"):
            add("archive_item", "live", harness.route(
                "archive_item", id=ids["archive_id"]))

    # First-track resolution: the pack lookup plus the playable() rewrite that
    # turns the first scraped URL into the URL handed to the player.
    kind = ids.get("rtp_cat_kind")
    cat_id = ids.get("rtp_cat_id")
    key = ids.get("rtp_cat_pack_key") or ids.get("rtp_pack_key")

    if key:
        def first_track():
            entry = _resolve_pack(key, kind=kind, cat_id=cat_id)
            tracks = (entry or {}).get("tracks") or []
            if not tracks:
                return {"status": 404, "items": 0, "bytes": 0}
            playable = legacy.playable(tracks[0]["url"])
            return {"status": 200, "items": len(tracks), "bytes": len(playable)}

        add("rtp_first_track", "replay/live", first_track)

    return scenarios


# --------------------------------------------------------------------------- run


def _record(run, meter):
    meter.reset()
    started = time.perf_counter()
    try:
        result = run() or {}
    except Exception as exc:  # a broken scenario must not kill the run
        result = {"status": 0, "items": 0, "bytes": 0,
                  "error": "%s: %s" % (exc.__class__.__name__, exc)}
    total = (time.perf_counter() - started) * 1000.0
    result = dict(result)
    result["total_ms"] = total
    result["upstream_ms"] = meter.upstream_ms
    result["cache_ms"] = meter.cache_ms
    result["parse_ms"] = max(0.0, total - meter.upstream_ms - meter.cache_ms)
    result["fetch_calls"] = len(meter.urls)
    result["urls"] = [(url, kind) for url, kind, _ms in meter.urls]
    return result


def _summarise(records):
    if not records:
        return {}
    keys = ("total_ms", "upstream_ms", "cache_ms", "parse_ms")
    summary = {}
    for key in keys:
        values = [row[key] for row in records]
        summary[key] = {
            "p50": round(percentile(values, 50), 2),
            "p95": round(percentile(values, 95), 2),
            "mean": round(statistics.mean(values), 2),
        }
    summary["items"] = max(row.get("items", 0) for row in records)
    summary["bytes"] = max(row.get("bytes", 0) for row in records)
    summary["status"] = records[-1].get("status", 0)
    summary["samples"] = len(records)
    summary["fetch_calls"] = round(
        statistics.mean(row.get("fetch_calls", 0) for row in records), 2)
    summary["errors"] = [row["error"] for row in records if row.get("error")]
    return summary


def run_scenarios(scenarios, cold_samples, warm_samples):
    meter = Meter()
    meter.install()
    output = []
    try:
        for name, mode, run in scenarios:
            cold = []
            for _ in range(cold_samples):
                _clear_caches()
                cold.append(_record(run, meter))
            warm = [_record(run, meter) for _ in range(warm_samples)]
            output.append({
                "name": name,
                "mode": mode,
                "cold": _summarise(cold),
                "warm": _summarise(warm),
            })
    finally:
        meter.uninstall()
    return output


def _print_table(results, platform):
    print()
    print("Platform: %s   cold samples: %d   warm samples: %d"
          % (platform, results[0]["cold"].get("samples", 0) if results else 0,
             results[0]["warm"].get("samples", 0) if results else 0))
    print()
    header = ("| Scenario | Mode | Items | Cold total p50 | Cold p95 | "
              "Cold upstream | Cold parse | Warm total p50 | Warm p95 | Warm cache | Payload |")
    print(header)
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in results:
        cold = row["cold"]
        warm = row["warm"]

        def ms(section, key, stat="p50"):
            value = (section or {}).get(key)
            if not isinstance(value, dict):
                return "-"
            return "%.0f" % value.get(stat, 0)

        print("| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            row["name"], row["mode"], cold.get("items", 0),
            ms(cold, "total_ms", "p50"), ms(cold, "total_ms", "p95"),
            ms(cold, "upstream_ms"), ms(cold, "parse_ms"),
            ms(warm, "total_ms", "p50"), ms(warm, "total_ms", "p95"),
            ms(warm, "cache_ms"),
            ("%d B" % cold["bytes"]) if cold.get("bytes") else "-",
        ))
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("auto", "web", "kodi"), default="auto")
    parser.add_argument("--replay", choices=("auto", "all", "none"), default="auto",
                        help="auto: archived RTP pages, live baselines (default)")
    parser.add_argument("--latency-ms", type=float, default=350.0,
                        help="simulated upstream latency for replayed pages")
    parser.add_argument("--cold-samples", type=int, default=1)
    parser.add_argument("--samples", type=int, default=5,
                        help="warm samples after the cold run")
    parser.add_argument("--json", default="", help="write full results to this path")
    parser.add_argument("--only", default="",
                        help="comma-separated scenario names to run")
    parser.add_argument("--cache-dir", default="",
                        help="reuse this cache dir instead of a throwaway one")
    args = parser.parse_args(argv)

    global _BENCH_CACHE
    if args.cache_dir:
        _BENCH_CACHE = os.path.abspath(os.path.expanduser(args.cache_dir))
        os.makedirs(_BENCH_CACHE, exist_ok=True)
        os.environ["RAVEPLAYER_CACHE_DIR"] = _BENCH_CACHE

    if args.platform != "auto" and args.platform != PLATFORM:
        parser.error("this tree is the %s build, not %s" % (PLATFORM, args.platform))

    restore = install_replay(args.replay, args.latency_ms)
    try:
        harness = WebHarness() if WEB else KodiHarness()
        ids = discover()
        scenarios = build_scenarios(harness, ids)
        if args.only:
            wanted = {name.strip() for name in args.only.split(",") if name.strip()}
            scenarios = [row for row in scenarios if row[0] in wanted]
        if not scenarios:
            parser.error("no scenarios selected")
        results = run_scenarios(scenarios, max(1, args.cold_samples),
                                max(1, args.samples))
    finally:
        if restore is not None:
            http_mod._request = restore

    payload = {
        "platform": PLATFORM,
        "replay": args.replay,
        "latency_ms": args.latency_ms,
        "cache_dir": _BENCH_CACHE,
        "ids": ids,
        "results": results,
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print("wrote %s" % args.json)
    _print_table(results, PLATFORM)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
