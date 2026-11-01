# -*- coding: utf-8 -*-
"""Curated Pirate Radio Archives (station -> archive entries).

Data is hand-researched and checked, so it lives in a JSON file rather than a
scraper: ``resources/pirate-radio.json``.  Every entry has exactly the shared
schema with the Kodi add-on:

    station, year, title, djs, source_url, stream_url, credit

``source_url`` is the page the recording/credit lives on and ``stream_url`` is
a direct audio file (only when one is confirmed).  When no URL could be
confirmed the metadata is kept, the URL is empty and ``credit`` carries the
note — the UI never invents a link.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "resources", "pirate-radio.json")
FIELDS = ("station", "year", "title", "djs", "source_url", "stream_url", "credit")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def entries():
    """Load and validate the curated entries (fail-soft: [])."""
    try:
        with open(DATA, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        entry = {field: raw.get(field, "") for field in FIELDS}
        if not all(isinstance(entry[field], str) for field in FIELDS):
            continue
        if not entry["station"] or not entry["title"]:
            continue
        result.append(entry)
    return result


def stations():
    """Group entries by station, preserving the curated order."""
    grouped = {}
    for entry in entries():
        key = slug(entry["station"])
        if not key:
            continue
        group = grouped.setdefault(key, {
            "id": key,
            "station": entry["station"],
            "count": 0,
            "years": [],
        })
        group["count"] += 1
        if entry["year"] and entry["year"] not in group["years"]:
            group["years"].append(entry["year"])
    for group in grouped.values():
        group["years"].sort()
    return list(grouped.values())


def station(key):
    """Entries for one station slug; [] when unknown."""
    wanted = slug(key)
    return [entry for entry in entries() if slug(entry["station"]) == wanted]
