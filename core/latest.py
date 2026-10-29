# -*- coding: utf-8 -*-
"""The "Latest" view: newest from every source, merged and date-sorted.

Sources emit different date shapes (RFC 822 from feeds, ISO from DNBRADIO,
year/date strings from the Archive), so everything is normalised to a
`datetime` here and undated items sort last.
"""

import datetime
import random
import re

from . import archive, dnbradio, feeds

RAVE_COLLECTION = "collection:ravetapes AND mediatype:audio"


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


def recent_episodes(per_feed=6):
    items = []
    for entry in feeds.registry():
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
    items = recent_episodes() + recent_packs() + recent_dnb()
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
