# -*- coding: utf-8 -*-
"""Live radio via radio-browser.info — a community directory that stays current."""

from . import fetch as http

MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]


def stations(tag, limit=100):
    """Return live, non-broken stations for a tag, most-clicked first."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 100
    query = (
        "/json/stations/search?tag=%s&limit=%d&hidebroken=true"
        "&order=clickcount&reverse=true" % (http.quote(str(tag)), limit)
    )
    for mirror in MIRRORS:
        data = http.fetch_json(mirror + query, timeout=20)
        if not isinstance(data, list):
            continue
        stations = [
            station for station in data
            if isinstance(station, dict) and stream_url(station)
        ]
        if stations:
            return stations
    return []


def stations_by_name(name, limit=20):
    """Live stations whose name matches (used for "Kool FM live now")."""
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    query = "/json/stations/search?name=%s&limit=%d&hidebroken=true&order=clickcount&reverse=true" % (
        http.quote(name), limit)
    for mirror in MIRRORS:
        data = http.fetch_json(mirror + query, timeout=20)
        if isinstance(data, list):
            return [station for station in data if isinstance(station, dict)]
    return []


def stream_url(station):
    if not isinstance(station, dict):
        return ""
    url = station.get("url_resolved") or station.get("url") or ""
    return url if isinstance(url, str) else ""


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def display(station):
    if not isinstance(station, dict):
        station = {}
    name = str(station.get("name") or "Unknown station").strip() or "Unknown station"
    country = str(station.get("countrycode") or station.get("country") or "").strip()
    codec = str(station.get("codec") or "").upper()
    bitrate = _as_int(station.get("bitrate"))
    meta = " · ".join(
        part for part in (country, codec, ("%d kbps" % bitrate) if bitrate > 0 else "") if part
    )
    return name, meta
