# -*- coding: utf-8 -*-
"""Parsing helpers for the curated third-party sources added in 1.6.14.

Moxser, Hardscore, Rave Archive UK and GetDarker are ordinary WordPress/RSS
sites plus one Craft CMS sitemap.  Keeping the network/parse split separate
means every adapter is testable from fixtures with no network at all.

Everything here is defensive: the input is third-party text, so malformed
markup returns empty values instead of raising.
"""

import email.utils
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

MEDIA_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp3|m4a|aac|wav|ogg)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
MIXCLOUD_RE = re.compile(r"https?://(?:www\.)?mixcloud\.com/[^\s\"'<>]+", re.IGNORECASE)


def clean_text(value, limit=300):
    """Unescape, collapse whitespace and cap a third-party string."""
    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_date(value):
    """Return ``YYYY-MM-DD`` from an RSS date, or "" when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d")


def _text(node, tag):
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _content(item):
    node = item.find(CONTENT_NS + "encoded")
    if node is not None and node.text:
        return node.text
    return _text(item, "description")


def media_urls(*values):
    """All http(s) audio URLs found in the given HTML/text blobs."""
    found = []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        for match in MEDIA_RE.finditer(value):
            url = html.unescape(match.group(0)).rstrip(").,;'\"")
            if url not in found:
                found.append(url)
    return found


def mixcloud_page(*values):
    """Return the cloudcast page URL embedded in HTML (widget ``feed=`` too)."""
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        text = html.unescape(value)
        for match in re.finditer(r"https?://(?:www\.)?mixcloud\.com/widget/iframe/[^\s\"'<>]*", text, re.I):
            url = html.unescape(match.group(0))
            query = urllib.parse.urlsplit(url).query
            feed = urllib.parse.parse_qs(query).get("feed")
            if feed and feed[0]:
                page = urllib.parse.unquote(feed[0])
                if "mixcloud.com/" in page:
                    return page
        for match in MIXCLOUD_RE.finditer(text):
            page = html.unescape(match.group(0)).rstrip(").,;'\"")
            path = urllib.parse.urlsplit(page).path
            if "/widget/" in path or not path.strip("/"):
                continue
            return page
    return ""


def parse_rss(xml_text, limit=50):
    """Parse an RSS 2.0 feed into plain dicts; malformed input returns [].

    Each item: title, link, date (YYYY-MM-DD), summary, content (raw HTML),
    media (list of direct audio URLs), mixcloud (cloudcast page URL or "").
    """
    if not isinstance(xml_text, str) or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff \t\r\n"))
    except (ET.ParseError, ValueError, TypeError):
        return []
    channel = root.find("channel")
    if channel is None:
        channel = root
    episodes = []
    for item in channel.findall("item")[:limit]:
        content = _content(item)
        enclosure = item.find("enclosure")
        enclosure_url = ""
        if enclosure is not None:
            enclosure_url = (enclosure.get("url") or "").strip()
        media = media_urls(content, enclosure_url)
        episodes.append({
            "title": clean_text(_text(item, "title")),
            "link": _text(item, "link"),
            "date": parse_date(_text(item, "pubDate")),
            "summary": clean_text(_text(item, "description"), 600),
            "content": content,
            "media": media,
            "mixcloud": mixcloud_page(content),
            "categories": [clean_text(category.text, 60)
                           for category in item.findall("category")][:8],
        })
    return episodes
