# -*- coding: utf-8 -*-
"""Warm the UK Raves parsed-page and provider-classification caches.

Run this once after a deploy or a container restart, and from a timer if the
room is used most days.  It performs exactly the work a first page view would
do — two category fetches plus the /go/mix classification — but off the request
path, so the first visitor gets a warm page instead of a two-to-eight second
cold one.

    # inside the container / on the host with the app's cache dir
    RAVEPLAYER_CACHE_DIR=/data/cache python scripts/warm_ukraves.py --pages 20

    # a quick single-page check
    RAVEPLAYER_CACHE_DIR=/data/cache python scripts/warm_ukraves.py --pages 1

Only pages 1..N are warmed; the catalogue is 169 app pages, so --pages 20
covers the pages the owner actually browses after a restart (~40 requests).
Set ``--workers`` to change the per-page redirect concurrency (default 4, the
value the provider tolerates without throttling).
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import ukraves  # noqa: E402


def warm_page(page, workers=ukraves.WORKERS):
    started = time.time()
    rows, has_next = ukraves.page_entries(page)
    described = ukraves.describe_many(rows, workers=workers)
    usable = [row for row in described
              if (row.get("provider") or {}).get("kind") in ("mediafire", "direct")]
    return {
        "page": page,
        "rows": len(rows),
        "usable": len(usable),
        "has_next": bool(has_next),
        "seconds": round(time.time() - started, 2),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--workers", type=int, default=ukraves.WORKERS)
    parser.add_argument("--json", default="", help="write the summary here")
    args = parser.parse_args(argv)
    if not os.environ.get("RAVEPLAYER_CACHE_DIR"):
        print("warning: RAVEPLAYER_CACHE_DIR is unset; warming the default cache",
              file=sys.stderr)
    summary = []
    for offset in range(max(0, args.pages)):
        page = max(1, args.start) + offset
        result = warm_page(page, workers=args.workers)
        summary.append(result)
        print("page %(page)3d rows=%(rows)2d usable=%(usable)2d next=%(has_next)s "
              "%(seconds).2fs" % result, file=sys.stderr)
    print("warmed %d page(s) in %.1fs" % (
        len(summary), sum(item["seconds"] for item in summary)), file=sys.stderr)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
