# -*- coding: utf-8 -*-
"""Tiny HTTP layer: standard library only, JSON in, friendly failures out."""

import codecs
import contextlib
import gzip
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import cache, kodi

USER_AGENT = "RavePlayer/3.0 (Kodi; +https://github.com/tcz009/plugin.audio.raveplayer)"
DEFAULT_TIMEOUT = 20

# One page at a time per host, with a minimum gap, for the scraped sources the
# site owners have asked us to go easy on.  The gate is only entered on a cache
# miss (``fetch`` returns before ``_request`` on a hit), and it wraps the whole
# request so two threads can never have two pages open to the same host.  This
# is deliberately per-process: a multi-worker deployment still caps each worker
# at one page, and the cache absorbs the duplicate work.
POLITE_HOSTS = {
    "www.ravetapepacks.com": 1.0,
    "ravetapepacks.com": 1.0,
}
_HOST_LOCKS = {}

# ---------------------------------------------------------------- RTP egress
# RaveTapePacks' edge firewall drops this network's WAN address at TCP level
# (SYN timeout), so no amount of retrying/referer work fixes the page fetch.
# When RTP_EGRESS_PROXY is set, requests to the hosts below - and only those -
# are sent through the homelab VPN host's HTTP CONNECT proxy.  Everything
# else keeps the normal direct path.  The proxy itself is host-allowlisted,
# so a redirect away from RTP goes direct and cannot turn it into an open
# proxy.  Every redirect hop is still validated by _SafeRedirectHandler.
EGRESS_PROXY_ENV = "RTP_EGRESS_PROXY"
EGRESS_HOSTS_ENV = "RTP_EGRESS_HOSTS"
EGRESS_FALLBACK_ENV = "RTP_EGRESS_FALLBACK"
DEFAULT_EGRESS_HOSTS = ("ravetapepacks.com",)


def _egress_hosts():
    raw = os.environ.get(EGRESS_HOSTS_ENV, "")
    hosts = tuple(
        host.strip().lower().lstrip(".") for host in raw.split(",") if host.strip()
    )
    return hosts or DEFAULT_EGRESS_HOSTS


def is_egress_host(value):
    """True when ``value`` (URL or hostname) belongs on the RTP egress route."""
    try:
        text = str(value or "").strip()
        if not text:
            return False
        if "://" in text:
            host = urllib.parse.urlsplit(text).hostname or ""
        else:
            host = text
        host = host.lower().rstrip(".")
    except (ValueError, AttributeError):
        return False
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in _egress_hosts())


def egress_proxy():
    """Return the configured proxy URL (``http://host:port``), or ``""``.

    ``host:port`` without a scheme is accepted for convenience.  Credentials,
    if any, stay inside the URL and are never logged.  An invalid value is
    treated as "not configured" so a typo cannot break every RTP fetch.
    """
    raw = (os.environ.get(EGRESS_PROXY_ENV, "") or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    if port is not None and not 1 <= port <= 65535:
        return ""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def egress_proxy_for(url):
    """Proxy URL for ``url`` when it is an RTP host, else ``""``."""
    if not is_egress_host(url):
        return ""
    return egress_proxy()


def _egress_fallback_enabled():
    value = (os.environ.get(EGRESS_FALLBACK_ENV, "1") or "1").strip().lower()
    return value not in ("0", "false", "no", "off")


class _EgressProxyHandler(urllib.request.ProxyHandler):
    """Per-request proxy routing: only the configured hosts use the proxy."""

    def proxy_open(self, req, proxy, type):
        if not is_egress_host(req.full_url):
            return None
        return urllib.request.ProxyHandler.proxy_open(self, req, proxy, type)


_EGRESS_OPENERS = {}
_EGRESS_OPENER_LOCK = threading.Lock()


def _egress_opener(proxy):
    with _EGRESS_OPENER_LOCK:
        opener = _EGRESS_OPENERS.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                _SafeRedirectHandler(),
                _EgressProxyHandler({"http": proxy, "https": proxy}),
            )
            _EGRESS_OPENERS[proxy] = opener
        return opener
_HOST_LAST_END = {}
_HOST_META_LOCK = threading.Lock()


@contextlib.contextmanager
def _host_slot(url):
    """Serialise requests to a polite host and keep ``gap`` seconds between."""
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        host = ""
    gap = POLITE_HOSTS.get(host)
    if not gap:
        yield
        return
    with _HOST_META_LOCK:
        lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    with lock:
        wait = gap - (time.monotonic() - _HOST_LAST_END.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        try:
            yield
        finally:
            _HOST_LAST_END[host] = time.monotonic()

# Redirects are followed by urllib, i.e. outside the stream proxy's SSRF guard.
# A third-party feed/API host could otherwise bounce the server into the LAN
# (169.254.169.254, 192.168.x.x, the Docker host, ...).  Every hop is re-checked
# against the same private-address rules the stream proxy uses, and the chain is
# capped.  Private targets stay reachable when RAVEWEB_ALLOW_PRIVATE=1, matching
# the stream proxy's opt-in.
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
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


def _read_limited(response, deadline, limit=MAX_BODY_BYTES):
    """Read a body in small chunks, enforcing a wall-clock deadline.

    ``HTTPResponse.read(n)`` blocks until it has n bytes, EOF or a *per-recv*
    socket timeout, so an upstream that drips bytes faster than the timeout can
    pin the worker long past the request timeout (review W2: 12 s read with
    timeout=2).  ``read1`` returns after one recv and the deadline is re-checked
    before every chunk; the 8 MiB cap is unchanged.
    """
    chunks = []
    total = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            kodi.log("response body read timed out", level=30)
            return None
        remaining = limit + 1 - total
        if remaining <= 0:
            kodi.log("refusing response over %d bytes" % limit, level=30)
            return None
        try:
            chunk = response.read1(min(READ_CHUNK_BYTES, remaining))
        except AttributeError:  # pragma: no cover - minimal fake responses
            chunk = response.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)


def _gunzip_limited(raw, deadline, limit=MAX_BODY_BYTES):
    data, total = [], 0
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                kodi.log("compressed response read timed out", level=30)
                return None
            chunk = stream.read1(READ_CHUNK_BYTES)
            if not chunk:
                return b"".join(data)
            data.append(chunk)
            total += len(chunk)
            if total > limit:
                kodi.log("refusing response over %d bytes after decompression"
                         % limit, level=30)
                return None


def _inflate_limited(raw, deadline, limit=MAX_BODY_BYTES):
    data, total = [], 0
    decompressor = zlib.decompressobj()
    pending = raw
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            kodi.log("compressed response read timed out", level=30)
            return None
        chunk = decompressor.decompress(pending, READ_CHUNK_BYTES)
        pending = decompressor.unconsumed_tail
        if chunk:
            data.append(chunk)
            total += len(chunk)
            if total > limit:
                kodi.log("refusing response over %d bytes after decompression"
                         % limit, level=30)
                return None
        if decompressor.eof or not chunk:
            return b"".join(data)


def _decode(response, deadline=None, limit=MAX_BODY_BYTES):
    """Read at most ``limit`` bytes within ``deadline`` and decode it.

    Without the cap a broken/hostile upstream could stream gigabytes into the
    worker's memory; without the deadline a slow-drip upstream could hold the
    worker for hours.  The default cap is unchanged; a known-large podcast feed
    opts in explicitly via :func:`fetch`'s ``max_bytes``.
    """
    limit = max(1, int(limit or MAX_BODY_BYTES))
    raw = _read_limited(response, deadline, limit)
    if raw is None:
        return None
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if "gzip" in encoding:
        raw = _gunzip_limited(raw, deadline, limit)
    elif encoding == "deflate":
        raw = _inflate_limited(raw, deadline, limit)
    if raw is None:
        return None
    if len(raw) > limit:
        kodi.log("refusing response over %d bytes after decompression" % limit,
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
    """HTTPRedirectHandler that re-validates every hop, caps the chain, and
    clamps each hop to the caller's remaining wall-clock budget (review W13)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        deadline = getattr(req, "raveweb_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            raise urllib.error.HTTPError(
                newurl, code, "redirect budget exhausted", headers, fp)
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
            if deadline is not None:
                new.raveweb_deadline = deadline
        return new

    def http_error_302(self, req, fp, code, msg, headers):
        # Clamp the next hop to what is left of the request budget; without
        # this every hop could consume the full provider timeout again.
        deadline = getattr(req, "raveweb_deadline", None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise urllib.error.HTTPError(
                    req.full_url, code, "redirect budget exhausted", headers, fp)
            req.timeout = max(0.5, min(getattr(req, "timeout", remaining), remaining))
        return super(_SafeRedirectHandler, self).http_error_302(
            req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_OPENER = urllib.request.build_opener(_SafeRedirectHandler())

# Transport failures that mean "the proxy/tunnel did not answer" (as opposed to
# an HTTP status from the target).  Only these trigger the direct fallback.
_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    socket.timeout,
    OSError,
    ValueError,
    EOFError,
    zlib.error,
)

# Why the most recent ``fetch`` on *this thread* returned None.  Callers need
# to tell "the source answered with an error" from "the network timed out":
# only the former is definitive enough to remember as a source-outage mark.
# A thread-local keeps parallel provider fetches from overwriting each other.
_FAILURE = threading.local()


def last_failure():
    """Return ``(kind, detail)`` for this thread's last failed fetch, or None.

    ``kind`` is ``"http"`` when the source itself answered (for example a 403
    from a bot filter), ``"timeout"`` for a connect/read deadline, or
    ``"transport"`` for anything else.  A successful fetch clears it.
    """
    return getattr(_FAILURE, "value", None)


def _set_failure(kind, detail=None):
    _FAILURE.value = (kind, detail) if kind else None


def _failure_kind(exc):
    """Classify a transport exception without hiding its HTTP status."""
    if isinstance(exc, urllib.error.HTTPError):
        return "http"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    return "transport"


def _request(url, timeout, headers, max_bytes=None):
    """Perform one request and return the decoded body, or None on failure.

    ``timeout`` is a budget for the whole fetch (connect + headers + body +
    redirects), not just one socket receive.  RTP hosts take the configured
    egress proxy; if that proxy is unreachable the request falls back to the
    direct path (the site still blocks it, but a stale proxy config must not
    break other diagnostics) within the same budget.
    """
    _set_failure(None)
    try:
        budget = max(0.5, float(timeout if timeout is not None else DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        budget = float(DEFAULT_TIMEOUT)
    deadline = time.monotonic() + budget
    proxy = egress_proxy_for(url)
    if proxy:
        request = _build_request(url, headers, deadline)
        try:
            body = _open_request(request, _egress_opener(proxy), budget,
                                 deadline, max_bytes or MAX_BODY_BYTES)
            _set_failure(None)
            return body
        except urllib.error.HTTPError as exc:
            _set_failure("http", getattr(exc, "code", None))
            _log_http_error(url, exc)
            return None
        except _TRANSPORT_ERRORS as exc:
            _set_failure(_failure_kind(exc), exc)
            kodi.log("RTP egress proxy unavailable for %s (%s); retrying direct"
                     % (_safe_url(url), type(exc).__name__), level=30)
            if not _egress_fallback_enabled():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                return None
            budget = remaining
            deadline = time.monotonic() + budget
    request = _build_request(url, headers, deadline)
    try:
        body = _open_request(request, _OPENER, budget, deadline,
                             max_bytes or MAX_BODY_BYTES)
        _set_failure(None)
        return body
    except urllib.error.HTTPError as exc:
        _set_failure("http", getattr(exc, "code", None))
        _log_http_error(url, exc)
        return None
    except _TRANSPORT_ERRORS as exc:
        _set_failure(_failure_kind(exc), exc)
        kodi.log("fetch failed: %s (%s)" % (_safe_url(url), exc), level=30)
        return None


def _build_request(url, headers, deadline):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    request.raveweb_deadline = deadline
    return request


def _open_request(request, opener, budget, deadline, limit=MAX_BODY_BYTES):
    with _host_slot(request.full_url):
        with opener.open(request, timeout=budget) as response:
            return _decode(response, deadline, limit)


def _log_http_error(url, exc):
    try:
        exc.close()
    except Exception:
        pass
    kodi.log("fetch failed: %s (%s)" % (_safe_url(url), exc), level=30)


def fetch(url, timeout=DEFAULT_TIMEOUT, headers=None, ttl=0, max_bytes=None):
    """Return the response body as text, or None on any failure.

    When ``ttl`` is greater than zero the body is cached on disk for that
    many seconds.  Cache failures are ignored.  ``max_bytes`` opts a caller
    with a known-large feed into a bigger read cap; the default stays
    :data:`MAX_BODY_BYTES` so an ordinary page cannot become a memory bomb.
    """
    limit = max(1, min(int(max_bytes or MAX_BODY_BYTES), 64 * 1024 * 1024))
    if max_bytes:
        request = lambda: _request(url, timeout, headers, limit)  # noqa: E731
    else:
        # Keep the historical three-argument call shape for callers/tests that
        # replace _request; only the explicit large-feed opt-in needs the cap.
        request = lambda: _request(url, timeout, headers)  # noqa: E731
    if not ttl:
        return request()
    key = _cache_key("text", url, headers)
    # sync=False: a fetched body is recomputable (a lost write re-fetches), and
    # an fsync per cold page is a visible disk stall on the NAS host.
    return cache.remember(key, ttl, request, sync=False)


def is_cached(url, ttl, headers=None):
    """True when ``fetch(url, ..., ttl=ttl)`` would be served from cache.

    Providers that stretch requests to <=1/s use this so a warm page (already
    cached) does not still sleep for the courtesy gap.  Classification is
    best-effort; a false negative only costs one extra throttle.
    """
    if not ttl:
        return False
    try:
        key = _cache_key("text", url, headers)
        return cache.has(key, ttl)
    except Exception:  # noqa: BLE001 - cache trouble must never break a fetch
        return False


def cached_body(url, ttl, headers=None):
    """Return the cached body for ``url`` without ever touching the network.

    Used where a page is nice-to-have (a category title taken from the already
    cached homepage nav) and a cold miss must not add a request.  Returns
    ``None`` when there is no fresh entry.
    """
    if not ttl:
        return None
    try:
        value = cache.get(_cache_key("text", url, headers), ttl)
    except Exception:  # noqa: BLE001 - cache trouble must never break a caller
        return None
    return None if value is cache.MISSING else value


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
        key, ttl,
        lambda: _parse_json(url, fetch(url, timeout=timeout, headers=headers)),
        sync=False,
    )


def quote(value):
    return urllib.parse.quote(value, safe="")
