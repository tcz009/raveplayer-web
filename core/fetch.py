# -*- coding: utf-8 -*-
"""Tiny HTTP layer: standard library only, JSON in, friendly failures out."""

import codecs
import gzip
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import cache, kodi

USER_AGENT = "RavePlayer/3.0 (Kodi; +https://github.com/tcz009/plugin.audio.raveplayer)"
DEFAULT_TIMEOUT = 20

# Redirects are followed by urllib, i.e. outside the stream proxy's SSRF guard.
# A third-party feed/API host could otherwise bounce the server into the LAN
# (169.254.169.254, 192.168.x.x, the Docker host, ...).  Every hop is re-checked
# against the same private-address rules the stream proxy uses, and the chain is
# capped.  Private targets stay reachable when RAVEWEB_ALLOW_PRIVATE=1, matching
# the stream proxy's opt-in.
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 8 * 1024 * 1024
ALLOW_PRIVATE_ENV = "RAVEWEB_ALLOW_PRIVATE"

_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in (
        "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24",
        "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
        "2001:db8::/32",
    )
)

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
    """Read at most MAX_BODY_BYTES and decode; returns None when too large.

    Without a cap a broken or hostile upstream could stream gigabytes into the
    worker's memory: archive/feeds/API bodies are only ever a few MB.
    """
    raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        kodi.log("refusing response over %d bytes" % MAX_BODY_BYTES, level=30)
        return None
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if "gzip" in encoding:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_BODY_BYTES + 1)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(raw, MAX_BODY_BYTES + 1)
        if decompressor.unconsumed_tail:
            kodi.log("refusing compressed response over %d bytes" % MAX_BODY_BYTES, level=30)
            return None
    if len(raw) > MAX_BODY_BYTES:
        kodi.log("refusing response over %d bytes after decompression" % MAX_BODY_BYTES,
                 level=30)
        return None
    return raw.decode(_charset(response.headers, raw), "replace")


def _cache_key(kind, url, headers):
    header_part = "&".join(
        "%s=%s" % (str(key).lower(), value)
        for key, value in sorted((headers or {}).items())
    )
    return "http:%s:%s:%s" % (kind, url, header_part)


def _safe_url(url):
    """Host/path only: never log query strings (API keys, signed URLs)."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "<unparsable url>"
    if not parts.scheme or not parts.netloc:
        return "<unparsable url>"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = parts.hostname or ""
    if port is not None:
        netloc = "%s:%d" % (netloc, port)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class BlockedRedirect(Exception):
    """A redirect pointed at a scheme/address the fetcher refuses."""


def _allow_private():
    return os.environ.get(ALLOW_PRIVATE_ENV, "") == "1"


def _address_blocked(address):
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified):
        return True
    return any(address in network for network in _BLOCKED_NETWORKS)


def _validate_redirect(url):
    """Return ``url`` if a redirect may be followed, else raise BlockedRedirect.

    Only http(s) is allowed and every resolved address must be public unless
    the operator opted into private hosts.  The initial URL is not checked: it
    comes from the app's own provider/registry code, not from a response.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise BlockedRedirect("unparsable redirect")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BlockedRedirect("bad redirect scheme or host")
    if parsed.username is not None or parsed.password is not None:
        raise BlockedRedirect("credentials in redirect")
    host = parsed.hostname
    if not host or len(host) > 253:
        raise BlockedRedirect("bad redirect host")
    try:
        port = parsed.port
    except ValueError:
        raise BlockedRedirect("bad redirect port")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port < 1 or port > 65535:
        raise BlockedRedirect("bad redirect port")
    if _allow_private():
        return url
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _address_blocked(literal):
            raise BlockedRedirect("private redirect address")
        return url
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise BlockedRedirect("redirect host could not be resolved")
    if not infos:
        raise BlockedRedirect("redirect host could not be resolved")
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise BlockedRedirect("unusable redirect address")
        if _address_blocked(address):
            raise BlockedRedirect("private redirect address")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler that re-validates every hop and caps the chain."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        count = getattr(req, "raveweb_redirects", 0) + 1
        if count > MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                newurl, code, "too many redirects", headers, fp)
        try:
            target = _validate_redirect(newurl)
        except BlockedRedirect as exc:
            kodi.log("refused redirect to %s (%s)" % (_safe_url(newurl), exc), level=30)
            raise urllib.error.HTTPError(
                newurl, code, "redirect to a blocked address", headers, fp)
        new = super(_SafeRedirectHandler, self).redirect_request(
            req, fp, code, msg, headers, target)
        if new is not None:
            new.raveweb_redirects = count
        return new


_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def _request(url, timeout, headers):
    """Perform one request and return the decoded body, or None on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return _decode(response)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        kodi.log("fetch failed: %s (%s)" % (_safe_url(url), exc), level=30)
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
        kodi.log("fetch failed: %s (%s)" % (_safe_url(url), exc), level=30)
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
        kodi.log("bad JSON from %s (%s)" % (_safe_url(url), exc), level=30)
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
