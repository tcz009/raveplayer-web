# -*- coding: utf-8 -*-
"""The "Latest" view: newest from every source, merged and date-sorted.

Sources emit different date shapes (RFC 822 from feeds, ISO from DNBRADIO,
year/date strings from the Archive), so everything is normalised to a
`datetime` here and undated items sort last.

Web-first latency fix: the feeds, Archive and DNBRADIO requests are
independent, so ``merge()`` runs them in a small bounded thread pool instead
of adding their timeouts together.  The Kodi add-on's copy still merges
sequentially today; mirror this change there deliberately if the add-on
starts using the same merge (its UI already paginates per source).
"""

import datetime
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import archive, dnbradio, feeds, kodi

RAVE_COLLECTION = "collection:ravetapes AND mediatype:audio"
# Every loader is independent (5 feeds + Archive + DNBRADIO = 7), so run one
# worker per loader: a cold /api/latest then costs the slowest provider (~25 s)
# instead of ceil(7/workers) rounds, which used to exceed the SPA's 40 s abort.
# The pool is still bounded (never more than the number of loaders).
MERGE_WORKERS = 7


def parse_date(value):
    """Best-effort date parsing across the sources; returns None if unknown."""
    if not value:
        return None
    text = str(value).strip()
    # ISO-ish: 2026-10-29, 2026-10-29T13:07:23Z
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return datetime.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    # RFC 822: Mon, 19 Jan 2026 11:51:21 +0000
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(text)
        if parsed:
            return parsed.replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    return None


def _feed_items(entry, per_feed=6):
    items = []
    for episode in feeds.episodes(entry["id"], limit=per_feed):
        when = parse_date(episode.get("published"))
        items.append({
            "kind": "episode",
            "title": episode["title"],
            "url": episode["url"],
            "source": entry["name"],
            "when": when,
            "duration": episode.get("duration"),
        })
    return items


def recent_episodes(per_feed=6):
    items = []
    for entry in feeds.registry():
        items.extend(_feed_items(entry, per_feed))
    return items


def recent_packs(rows=20):
    docs, _ = archive.search(RAVE_COLLECTION, rows=rows, sort="addeddate desc")
    items = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        items.append({
            "kind": "pack",
            "title": doc.get("title") or identifier,
            "identifier": identifier,
            "source": "Rave Tape Packs",
            "when": parse_date(doc.get("addeddate") or doc.get("date")),
            "year": doc.get("year"),
        })
    return items


def recent_dnb(limit=12):
    items = []
    for show in dnbradio.mixes()[:limit]:
        items.append({
            "kind": "dnb",
            "title": show["title"],
            "id": show["id"],
            "source": "DNB Radio",
            "when": parse_date(show.get("date")),
        })
    return items


def merge(limit=60):
    """Newest items from every source, date-sorted, undated last.

    Each feed and the Archive/DNBRADIO calls are independent, so they run in
    a bounded pool.  A provider that raises is logged and contributes no
    items; the others still land.  Results are reassembled in the old
    source order so equal timestamps keep their previous ordering.
    """
    loaders = [
        ("podcast:%s" % entry["id"], (lambda entry=entry: _feed_items(entry)))
        for entry in feeds.registry()
    ]
    loaders.append(("packs", recent_packs))
    loaders.append(("dnb", recent_dnb))

    found = {}
    with ThreadPoolExecutor(max_workers=min(MERGE_WORKERS, len(loaders))) as pool:
        futures = {pool.submit(loader): name for name, loader in loaders}
        for future in as_completed(futures):
            name = futures[future]
            try:
                found[name] = list(future.result() or [])
            except Exception as exc:  # one dead source must not hide the rest
                found[name] = []
                kodi.log("latest: ignoring %s (%s)" % (name, exc), level=30)

    items = []
    for name, _loader in loaders:
        items.extend(found.get(name, []))
    dated = [item for item in items if item.get("when")]
    undated = [item for item in items if not item.get("when")]
    dated.sort(key=lambda item: item["when"], reverse=True)
    return (dated + undated)[:limit]


def random_pack():
    """Pick a random tape pack. Uses a random page so we do not bias to page 1."""
    _, total = archive.search(RAVE_COLLECTION, rows=1)
    if not total:
        return None
    per_page = 50
    page = random.randint(1, max(1, min(200, total // per_page)))
    docs, _ = archive.search(RAVE_COLLECTION, page=page, rows=per_page)
    if not docs:
        return None
    doc = random.choice(docs)
    return {
        "identifier": doc.get("identifier"),
        "title": doc.get("title") or doc.get("identifier"),
    }


def year_query(year):
    return "%s AND date:[%d-01-01 TO %d-12-31]" % (RAVE_COLLECTION, year, year)
