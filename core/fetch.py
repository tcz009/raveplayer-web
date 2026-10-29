# -*- coding: utf-8 -*-
"""Tiny HTTP layer: standard library only, JSON in, friendly failures out."""

import codecs
import gzip
import http.client
import io
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import cache, kodi

USER_AGENT = "RavePlayer/3.0 (Kodi; +https://github.com/tcz009/plugin.audio.raveplayer)"
DEFAULT_TIMEOUT = 20

_XML_ENCODING_RE = re.compile(br"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)""", re.IGNORECASE)


def _charset(headers, raw):
    """Pick a text encoding, preferring a BOM or XML declaration.

    A surprising number of podcast feeds are served as ``application/xml``
    without a charset but declare ISO-8859-1 inside the document.  Trusting
    UTF-8 unconditionally mangles those titles.
    """
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    charset = headers.get_content_charset()
    if not charset:
        match = _XML_ENCODING_RE.match(raw[:256])
        if match:
            charset = match.group(1).decode("ascii", "replace")
    if not charset:
        return "utf-8"
    try:
        codecs.lookup(charset)
    except LookupError:
        kodi.log("unknown charset %r; falling back to utf-8" % charset)
        return "utf-8"
    return charset


def _decode(response):
    raw = response.read()
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if "gzip" in encoding:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read()
    elif encoding == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode(_charset(response.headers, raw), "replace")


def _cache_key(kind, url, headers):
    header_part = "&".join(
        "%s=%s" % (str(key).lower(), value)
        for key, value in sorted((headers or {}).items())
    )
    return "http:%s:%s:%s" % (kind, url, header_part)


def _request(url, timeout, headers):
    """Perform one request and return the decoded body, or None on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _decode(response)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        kodi.log("fetch failed: %s (%s)" % (url, exc), level=30)
        return None
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        socket.timeout,
        OSError,
        ValueError,
        EOFError,
        zlib.error,
    ) as exc:
        kodi.log("fetch failed: %s (%s)" % (url, exc), level=30)
        return None


def fetch(url, timeout=DEFAULT_TIMEOUT, headers=None, ttl=0):
    """Return the response body as text, or None on any failure.

    When ``ttl`` is greater than zero the body is cached on disk for that
    many seconds.  Cache failures are ignored.
    """
    if not ttl:
        return _request(url, timeout, headers)
    key = _cache_key("text", url, headers)
    return cache.remember(key, ttl, lambda: _request(url, timeout, headers))


def _parse_json(url, body):
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        kodi.log("bad JSON from %s (%s)" % (url, exc), level=30)
        return None


def fetch_json(url, timeout=DEFAULT_TIMEOUT, headers=None, ttl=0):
    """Fetch and parse JSON, or return None when the server misbehaves."""
    if not ttl:
        return _parse_json(url, fetch(url, timeout=timeout, headers=headers))
    key = _cache_key("json", url, headers)
    return cache.remember(
        key, ttl, lambda: _parse_json(url, fetch(url, timeout=timeout, headers=headers))
    )


def quote(value):
    return urllib.parse.quote(value, safe="")
