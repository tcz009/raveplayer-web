# -*- coding: utf-8 -*-
"""Internet Archive provider.

The Archive's advancedsearch and metadata endpoints are public, stable and
need no key, which makes them a much better backbone than scraping the dead
fan sites the 2014 add-on relied on.
"""

import re
import urllib.parse

from . import fetch as http

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/%s"
DOWNLOAD_URL = "https://archive.org/download/%s/%s"
THUMB_URL = "https://archive.org/services/img/%s"

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".opus")
FIELDS = ["identifier", "title", "year", "date", "addeddate", "downloads", "mediatype", "creator", "description"]

SEARCH_TTL = 300
ITEM_TTL = 3600


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


def thumb(identifier):
    return THUMB_URL % _quote(identifier)


def _as_text(value):
    """Collapse whitespace and turn Archive's occasional lists into text."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value)
    return " ".join(str(value or "").split())


def search(query, page=1, rows=50, sort="downloads desc"):
    """Return (docs, total) for an Archive.org full-text query."""
    params = [
        ("q", query),
        ("rows", str(rows)),
        ("page", str(page)),
        ("output", "json"),
        ("sort[]", sort),
    ]
    params += [("fl[]", field) for field in FIELDS]
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    data = http.fetch_json(url, timeout=25, ttl=SEARCH_TTL)
    if not isinstance(data, dict):
        return [], 0
    response = data.get("response")
    if not isinstance(response, dict):
        return [], 0
    docs = response.get("docs")
    if not isinstance(docs, list):
        docs = []
    docs = [doc for doc in docs if isinstance(doc, dict)]
    try:
        total = int(response.get("numFound") or 0)
    except (TypeError, ValueError):
        total = 0
    return docs, max(0, total)


def item(identifier):
    """Return the metadata dict for one Archive.org item, or {}."""
    identifier = str(identifier or "")
    if not identifier:
        return {}
    data = http.fetch_json(METADATA_URL % _quote(identifier), timeout=25, ttl=ITEM_TTL)
    return data if isinstance(data, dict) else {}


def audio_files(identifier):
    """Return playable audio files for an item, best format first, de-duplicated."""
    data = item(identifier)
    files = data.get("files")
    if not isinstance(files, list):
        files = []
    by_name = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        lower = name.lower()
        if not lower.endswith(AUDIO_EXTENSIONS):
            continue
        if lower.endswith(("_spectrogram.png", "_sample.mp3")):
            continue
        # The Archive lists both the original and derived files; prefer the
        # original of each track and drop duplicates that differ only by dir.
        key = lower.split("/")[-1]
        existing = by_name.get(key)
        rank = _rank(entry)
        if existing is None or rank < existing[0]:
            by_name[key] = (rank, entry)
    ordered = [entry for _, entry in sorted(by_name.values(), key=lambda pair: pair[0])]
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    item_title = _as_text(metadata.get("title"))
    return [
        {
            "name": entry.get("name"),
            "title": _track_title(entry, item_title),
            "size": _as_int(entry.get("size")),
            "length": entry.get("length"),
            "url": DOWNLOAD_URL % (_quote(identifier), urllib.parse.quote(entry["name"], safe="/")),
        }
        for entry in ordered
    ]


def _track_title(entry, item_title):
    """Return something a human can pick from a list.

    Archive rips are inconsistent: sometimes the only useful label is the
    filename ("TAPE 3 - MASTERVIBE.mp3"), sometimes every file repeats the
    same generic title ("Dreamscape XIX Arena 2"). Prefer a descriptive
    filename, then a distinct per-track title, then a tidied filename.
    """
    pretty = _pretty(entry.get("name"))
    title = _as_text(entry.get("title"))
    if _is_descriptive(pretty):
        return pretty
    if title and title != item_title:
        return title
    return pretty


def _is_descriptive(text):
    text = _as_text(text)
    if not text or len(text) < 5:
        return False
    if re.match(r"^(track|audio|file|part)?\s*\d+$", text, re.IGNORECASE):
        return False
    return bool(re.search(r"[A-Za-z]", text))


def _rank(entry):
    source = str(entry.get("source") or "").lower()
    if source == "original":
        return 0
    fmt = str(entry.get("format") or "").lower()
    if fmt in ("vbr mp3", "mp3"):
        return 1
    return 2


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pretty(name):
    text = _as_text(name)
    if not text:
        return "Untitled"
    base = text.split("/")[-1]
    for extension in AUDIO_EXTENSIONS:
        if base.lower().endswith(extension):
            base = base[: -len(extension)]
            break
    return base.replace("_", " ").strip() or "Untitled"
