# -*- coding: utf-8 -*-
"""DNBRADIO.com archive — present-day jungle and drum & bass shows.

The recordings index lists recent mixes with ids; each listen page carries a
direct MP3 behind an archive proxy, so the stream URL is resolved on demand.
"""

import html as html_module
import re
import urllib.parse

from . import fetch as http

BASE = "https://dnbradio.com"
RECORDINGS = BASE + "/recordings"
RECORDINGS_TTL = 10 * 60

MIX_RE = re.compile(r'href="/listen\?mix=(\d+)"\s+title="([^"]+)"', re.IGNORECASE)
MP3_RE = re.compile(r'(?:href|src)="(https?://[^"]+?\.mp3[^"]*)"', re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_recordings(body):
    """Parse the recordings page HTML into mix dicts (no I/O)."""
    if not body:
        return []
    seen, result = set(), []
    for mix_id, title in MIX_RE.findall(body):
        if mix_id in seen:
            continue
        seen.add(mix_id)
        clean = html_module.unescape(title).replace(" on DNBRADIO", "").strip()
        date, separator, show = clean.partition(" : ")
        # The page's title attribute is "2026-10-29 : DJ - Show".  If a page
        # ever loses the separator, do not present the whole string as both the
        # title and the date (mirrors the add-on's DATE_RE guard).
        if separator and DATE_RE.match(date.strip()):
            result.append({
                "id": mix_id,
                "title": (show.strip() or clean) or "Untitled",
                "date": date.strip(),
            })
        else:
            result.append({"id": mix_id, "title": clean or "Untitled", "date": ""})
    return result


def mixes():
    return parse_recordings(
        http.fetch(RECORDINGS, timeout=25, headers={"Referer": BASE + "/"}, ttl=RECORDINGS_TTL)
    )


def parse_listen(page):
    """Extract the MP3 URL from a listen page HTML (no I/O)."""
    if not page:
        return ""
    match = MP3_RE.search(page)
    if not match:
        return ""
    return html_module.unescape(match.group(1))


def stream_url(mix_id):
    """Resolve a mix id to a playable MP3 URL, or '' if the page changed."""
    return parse_listen(http.fetch(link(mix_id), timeout=25, headers={"Referer": RECORDINGS}))


def link(mix_id):
    return "%s/listen?mix=%s" % (BASE, urllib.parse.quote(str(mix_id)))
