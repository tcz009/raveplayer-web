#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live audit: do the MP3 URLs the parsers build actually resolve?

Samples links straight from the provider parsers, requests each one with the
same Referer/User-Agent the app sends (plus a Range), and reports a pass rate.
Run from the repo root:

    python3 scripts/audit_legacy_links.py --limit 10
    python3 scripts/audit_legacy_links.py --source jungletapes --limit 5
    python3 scripts/audit_legacy_links.py --bare   # show the hotlink-403 delta

Exit code is 1 when the pass rate is below ``--min-pass`` (default 0.9) so CI
or a cron job can alert.  This script is not imported by the test suite.
"""

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(ROOT, "core")):
    sys.path.insert(0, ROOT)
    from core import hardscore, legacy  # noqa: E402
    WEB = True
else:
    sys.path.insert(0, os.path.join(ROOT, "test", "stubs"))
    sys.path.insert(0, ROOT)
    from resources.lib import hardscore, legacy  # noqa: E402
    WEB = False

BROWSER_UA = "Mozilla/5.0"
DEFAULT_TIMEOUT = 25
MAX_BYTES = 2


def _headers_for(url, bare=False):
    """Return (base_url, headers) from a Kodi-suffixed URL."""
    base, separator, suffix = str(url).partition("|")
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if separator:
        for pair in suffix.split("&"):
            name, _sep, value = pair.partition("=")
            if name:
                headers[name] = urllib.parse.unquote(value)
    if bare:
        headers.pop("Referer", None)
        headers["User-Agent"] = "RavePlayer-Audit/1.0"
    elif "Referer" not in headers:
        split = urllib.parse.urlsplit(base)
        headers["Referer"] = "%s://%s/" % (split.scheme or "https", split.netloc)
    headers["Range"] = "bytes=0-%d" % (MAX_BYTES - 1)
    return base, headers


def probe(url, timeout, bare=False):
    base, headers = _headers_for(url, bare=bare)
    request = urllib.request.Request(base, headers=headers)
    result = {"url": url, "status": 0, "type": "", "range": "", "final": base,
              "error": "", "bytes": 0}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["status"] = getattr(response, "status", response.getcode())
            result["final"] = response.geturl()
            result["type"] = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            result["range"] = response.headers.get("Content-Range") or ""
            result["bytes"] = len(response.read(MAX_BYTES))
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["final"] = getattr(exc, "url", base)
        result["type"] = (exc.headers.get("Content-Type") or "").split(";")[0].strip().lower() if exc.headers else ""
        result["error"] = "HTTP %d" % exc.code
    except Exception as exc:  # noqa: BLE001 - the audit reports, never raises
        result["error"] = "%s: %s" % (exc.__class__.__name__, exc)
    result["ok"] = result["status"] in (200, 206) and result["type"].startswith("audio/")
    return result


def ravetapepacks(limit, timeout):
    tracks = [track for pack in legacy.packs() for track in pack.get("tracks") or []]
    return [(track["url"], legacy.playable(track["url"])) for track in tracks[:limit]]


def jungletapes(limit, timeout):
    shows = [show for show in legacy.jungle_shows() if show.get("key")]
    tracks = []
    for show in shows[:3]:
        tracks.extend(legacy.jungle_tracks(show["key"]))
        if len(tracks) >= limit:
            break
    return [(track["url"], track["url"]) for track in tracks[:limit]]


def hardscore_links(limit):
    if WEB:
        rows = hardscore.mixes(limit=50)
        urls = [row.get("media") for row in rows if row.get("media")]
    else:
        rows = hardscore.items(limit=50)
        urls = [url for row in rows for url in row.get("media_urls") or []]
    return [(url, legacy.playable(url)) for url in urls[:limit]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="all",
                        choices=("all", "ravetapepacks", "jungletapes", "hardscore"))
    parser.add_argument("--limit", type=int, default=10, help="links to sample per source")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--min-pass", type=float, default=0.9,
                        help="exit non-zero below this pass rate (0..1)")
    parser.add_argument("--bare", action="store_true",
                        help="also request without the Referer (hotlink delta)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    random.seed(args.seed)

    builders = {
        "ravetapepacks": lambda: ravetapepacks(args.limit, args.timeout),
        "jungletapes": lambda: jungletapes(args.limit, args.timeout),
        "hardscore": lambda: hardscore_links(args.limit),
    }
    selected = builders if args.source == "all" else {args.source: builders[args.source]}
    report = {}
    for source, build in selected.items():
        try:
            pairs = build()
        except Exception as exc:  # noqa: BLE001 - a dead provider is 0% not a traceback
            report[source] = {"error": "%s: %s" % (exc.__class__.__name__, exc),
                              "total": 0, "passed": 0, "rate": 0.0, "rows": []}
            continue
        rows = []
        for raw_url, play_url in pairs:
            row = probe(play_url, args.timeout)
            row["source_url"] = raw_url
            if args.bare:
                row["bare"] = probe(play_url, args.timeout, bare=True)
            rows.append(row)
        passed = sum(1 for row in rows if row["ok"])
        report[source] = {
            "total": len(rows),
            "passed": passed,
            "rate": (passed / len(rows)) if rows else 0.0,
            "rows": rows,
        }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for source, data in report.items():
            if data.get("error"):
                print("%-14s ERROR %s" % (source, data["error"]))
                continue
            print("%-14s %d/%d pass (%.0f%%)"
                  % (source, data["passed"], data["total"], 100 * data["rate"]))
            for row in data["rows"]:
                mark = "OK " if row["ok"] else "FAIL"
                line = "  %s %3s %-16s %s" % (
                    mark, row["status"], row["type"] or "-", row["final"])
                if row.get("bare") is not None:
                    bare = row["bare"]
                    line += "  [bare %s %s]" % (bare["status"], bare["type"] or "-")
                if row["error"]:
                    line += "  (%s)" % row["error"]
                print(line)
    rates = [data["rate"] for data in report.values() if data.get("total")]
    return 0 if rates and min(rates) >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
