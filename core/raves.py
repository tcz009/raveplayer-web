# -*- coding: utf-8 -*-
"""Researched catalogue of classic UK rave promotions, venues and stations.

Data lives in resources/rave-catalog.json, produced by the research pass in
docs/RESEARCH-RAVES.md: every name was checked against the Internet Archive
and Mixcloud, and only tested query strings are stored here.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "resources", "rave-catalog.json")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def registry():
    try:
        with open(CATALOG, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        entries.append({
            "name": name,
            "slug": slug(name),
            "type": entry.get("type", ""),
            "archive_query": entry.get("archive_query", ""),
            "mixcloud_query": entry.get("mixcloud_query", ""),
            "archive_count": int(entry.get("archive_count") or 0),
            "mixcloud_count": int(entry.get("mixcloud_count") or 0),
        })
    return entries


def entry(key):
    return next((item for item in registry() if item["slug"] == key), None)


def with_archive():
    return [item for item in registry() if item["archive_count"] > 0 and item["archive_query"]]
