# -*- coding: utf-8 -*-
"""DNBForum "DJ EZ / Old Skool Garage Sets" — 23 Mediafire files.

The DNBForum thread is behind Cloudflare, so the links below were curated from
the Wayback Machine snapshot of
https://dnbforum.com/threads/ez-old-skool-garage-sets.43637/ .  Each link is a
Mediafire file page; the page embeds a short-lived signed
``download*.mediafire.com`` URL.  Mediafire rate-limits bursts with HTTP 429,
so this module resolves one page at a time, keeps a minimum gap between
requests, caches a signed URL for two hours, caches failures briefly and backs
off for as long as ``Retry-After`` asks.

The web build calls this only.  The Kodi add-on has no equivalent because the
signed URL is short-lived and the thread itself is Cloudflare-protected.
"""

import base64
import email.utils
import html
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCE_URL = "https://dnbforum.com/threads/ez-old-skool-garage-sets.43637/"
SOURCE_LABEL = "DNBForum"
MEDIAFIRE_HOST = "www.mediafire.com"
PAGE_PATH = "https://www.mediafire.com/file/%s/x.mp3"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Signed download URLs last a few hours; Mediafire does not document an exact
# lifetime, so stay comfortably inside it and re-resolve on a miss.
TTL = 2 * 60 * 60
NEGATIVE_TTL = 5 * 60
# One page at a time, with a real gap.  The whole resolve path is serialised by
# _LOCK, so this also acts as the single-flight gate for concurrent requests.
MIN_GAP = 1.2
MAX_BACKOFF = 120
MAX_PAGE_BYTES = 4 * 1024 * 1024
TIMEOUT = 25

SETS = [
    {"id": "72aswcrx4hlw2sh", "title": "DJ EZ @ La Cosa Nostra"},
    {"id": "bavkbz5zvku5284", "title": "DJ EZ - Pure Flava Summer 1999"},
    {"id": "2n5wfiba526ju85", "title": "DJ EZ & Ends Live PA - Sidewinder Summerball 2003"},
    {"id": "fktw2ppaznwy28l", "title": "DJ EZ + Martin Larner @ TPCAW 04 D4PR"},
    {"id": "ogttfi4itkcb4vx", "title": "DJ EZ - Garage Nation Halloween Ball"},
    {"id": "1ir76yp3z3mk467", "title": "DJ EZ Part 1 - Sidewinder Valentines Showcase 2002"},
    {"id": "zeh2llk9cqf4yht", "title": "DJ EZ Part 2 - Sidewinder Valentines Showcase"},
    {"id": "a3r7qe9b67fetr8", "title": "DJ EZ - Sidewinder NYE 2002 @ Enigma, Bedford"},
    {"id": "2x35r0l6nia5dda", "title": "DJ EZ - Doneao & Dappa @ Mission, Bedford 28.04.04"},
    {"id": "4i738v15dzh7od2", "title": "DJ EZ - Mighty Moe, CKP, Bushkin @ Sidewinder Club Tour 17.10.03"},
    {"id": "4l8uqw55cdwus66", "title": "DJ EZ - CKP, Blakey & Viper @ Sidewinder 3rd B'day Bash 2002"},
    {"id": "lrtcdatoli158dq", "title": "DJ EZ - Kie & B-Live @ Sidewinder Bonfire Bonanza 2002"},
    {"id": "0eji9dmnebad27g", "title": "DJ EZ - Kie & Sharky P @ Exposure"},
    {"id": "7hmsar805bijkl2", "title": "DJ EZ Live @ 4x4"},
    {"id": "rd291jn168oj16q", "title": "DJ EZ - @ Back 2 95 Nov 2011"},
    {"id": "4hs01tnzs45p3u4", "title": "DJ EZ Sparks B'day"},
    {"id": "lnl0clz73d5yocm", "title": "DJ EZ @ Back 2 95"},
    {"id": "0cjeztm44bvr4wl", "title": "DJ EZ @ Liberty [Todd Edwards Mix]"},
    {"id": "qec8xqr6ze204r9", "title": "DJ EZ @ Scala Boxing Day 2005"},
    {"id": "9cxs488xz8sqb7r", "title": "DJ EZ @ Fabric 2011"},
    {"id": "vzq2nwtgruj", "title": "DJ EZ + Kofi B @ Z Uncut - Ice Club Ayia Napa July 2006"},
    {"id": "nqcz5hsp68m91qo", "title": "DJ EZ @ Lovebugs 2Stush"},
    {"id": "zsla8z4f8d7v721", "title": "DJ EZ @ United Dance 2003"},
]
SET_INDEX = {entry["id"]: entry for entry in SETS}

FILE_ID_RE = re.compile(r"^[a-z0-9]{8,32}$")
# A signed URL: download###.mediafire.com/<dkey>/<file id>/<name>.mp3?...
_SIGNED_RE = re.compile(
    r"""https?://(?:[a-z0-9-]+\.)*mediafire\.com/[^"'\s<>\\]+?\.(?:mp3|m4a)"""
    r"""(?:\?[^"'\s<>\\]*)?""",
    re.I,
)
_SCRAMBLED_RE = re.compile(
    r"""data-scrambled-url=["']([^"']+)["']""", re.I)


class EzGarageError(Exception):
    """A typed resolver failure the web view can turn into a status code."""

    code = 502

    def __init__(self, message, code=None, retry_after=None):
        super(EzGarageError, self).__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = int(code)
        self.retry_after = retry_after


class UnknownSet(EzGarageError):
    code = 404


class MediafireRateLimited(EzGarageError):
    code = 503


class MediafireUnavailable(EzGarageError):
    code = 502


class MediafireFormatChanged(EzGarageError):
    code = 502


_LOCK = threading.RLock()
_CACHE = {}
_LAST_FETCH = 0.0
_BACKOFF_UNTIL = 0.0
_SLEEP = time.sleep
_CLOCK = time.monotonic


def clear_memory():
    """Drop the TTL/negative cache and the rate-limit state (tests/ops)."""
    global _LAST_FETCH, _BACKOFF_UNTIL
    with _LOCK:
        _CACHE.clear()
        _LAST_FETCH = 0.0
        _BACKOFF_UNTIL = 0.0


def entry(set_id):
    """The curated row for ``set_id``; raises :class:`UnknownSet` otherwise."""
    wanted = str(set_id or "").strip().lower()
    row = SET_INDEX.get(wanted)
    if row is None:
        raise UnknownSet("unknown DJ EZ set")
    return row


def page_url(set_id):
    """Canonical Mediafire page URL for a curated set."""
    row = entry(set_id)
    return PAGE_PATH % urllib.parse.quote(row["id"], safe="")


def set_id_for_url(value):
    """Return a curated set id found in a page or signed URL, else ``""``."""
    try:
        path = urllib.parse.urlsplit(str(value or "")).path
    except (TypeError, ValueError):
        return ""
    for part in path.split("/"):
        if part in SET_INDEX:
            return part
    return ""


def _cache_entry(set_id):
    now = _CLOCK()
    cached = _CACHE.get(set_id)
    if cached is None:
        return None
    if cached.get("expires", 0.0) <= now:
        _CACHE.pop(set_id, None)
        return None
    return cached


def _remember_url(set_id, url):
    _CACHE[set_id] = {"url": url, "expires": _CLOCK() + TTL}


def _remember_error(set_id, error):
    ttl = NEGATIVE_TTL
    if isinstance(error, MediafireRateLimited):
        ttl = max(1, min(int(error.retry_after or 1), MAX_BACKOFF))
    _CACHE[set_id] = {
        "error": type(error),
        "message": error.message,
        "code": error.code,
        "retry_after": error.retry_after,
        "expires": _CLOCK() + ttl,
    }


def _raise_cached(cached):
    error_type = cached.get("error")
    if error_type is None:
        return
    raise error_type(cached.get("message") or "Mediafire resolve failed",
                     code=cached.get("code"),
                     retry_after=cached.get("retry_after"))


def _retry_after(headers, default=30):
    value = ""
    for name, header in (headers or {}).items():
        if str(name).lower() == "retry-after":
            value = str(header or "").strip()
            break
    if not value:
        return default
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            when = email.utils.parsedate_to_datetime(value)
            seconds = int(when.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            seconds = default
    return max(1, min(seconds, MAX_BACKOFF))


def _wait_for_slot():
    """Honour an active 429 backoff, then keep one-page-per-gap pacing."""
    global _LAST_FETCH
    now = _CLOCK()
    if _BACKOFF_UNTIL > now:
        raise MediafireRateLimited(
            "Mediafire is rate-limiting requests; try again shortly",
            retry_after=max(1, int(math.ceil(_BACKOFF_UNTIL - now))))
    gap = MIN_GAP - (now - _LAST_FETCH)
    if gap > 0:
        _SLEEP(gap)


def _fetch_page(url):
    """GET a Mediafire page.  Returns ``(status, headers, text)``.

    HTTP errors are returned (so 429 is data, not an exception); transport
    errors become :class:`MediafireUnavailable`.  Tests replace this function.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Referer": SOURCE_URL,
    })
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - close is best effort
            pass
        return int(exc.code or 0), headers, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MediafireUnavailable(
            "Mediafire could not be reached (%s)" % type(exc).__name__)
    try:
        status = int(getattr(response, "status", 200) or 200)
        headers = dict(response.headers.items()) if response.headers else {}
        raw = response.read(MAX_PAGE_BYTES + 1)
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - close is best effort
            pass
    if len(raw) > MAX_PAGE_BYTES:
        raise MediafireUnavailable("Mediafire returned an oversized page")
    return status, headers, raw.decode("utf-8", "replace")


def _decode_scrambled(text):
    """Mediafire sometimes base64-encodes the direct URL in a data attribute."""
    for token in _SCRAMBLED_RE.findall(text or ""):
        token = token.strip()
        if not token:
            continue
        padded = token + ("=" * (-len(token) % 4))
        try:
            decoded = base64.b64decode(padded).decode("utf-8", "replace")
        except (TypeError, ValueError):
            continue
        if "mediafire.com" in decoded:
            text += "\n" + decoded
    return text


def _valid_signed_url(value):
    """A direct Mediafire download URL on a download* subdomain, or ``""``."""
    if not isinstance(value, str):
        return ""
    candidate = html.unescape(value).replace("\\/", "/").strip()
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    host = parts.hostname.lower().rstrip(".")
    if host in ("mediafire.com", MEDIAFIRE_HOST):
        return ""
    if not host.endswith(".mediafire.com"):
        return ""
    if not parts.path.lower().endswith((".mp3", ".m4a")):
        return ""
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, ""))


def signed_url_from_page(body):
    """Extract the signed Mediafire URL from a file page, or return ``""``."""
    if not isinstance(body, str) or not body:
        return ""
    text = html.unescape(_decode_scrambled(body))
    text = text.replace("\\/", "/")
    for match in _SIGNED_RE.findall(text):
        valid = _valid_signed_url(match)
        if valid:
            return valid
    return ""


def resolve(set_id):
    """Return a direct signed Mediafire URL for one curated set.

    Raises :class:`UnknownSet`, :class:`MediafireRateLimited`,
    :class:`MediafireUnavailable` or :class:`MediafireFormatChanged`.  The
    signed URL is cached for :data:`TTL`; failures are cached briefly, and an
    HTTP 429 parks every resolve until the server's ``Retry-After``.
    """
    global _LAST_FETCH, _BACKOFF_UNTIL
    row = entry(set_id)  # unknown ids never touch the network
    wanted = row["id"]

    with _LOCK:
        cached = _cache_entry(wanted)
        if cached is not None:
            if cached.get("url"):
                return cached["url"]
            _raise_cached(cached)

        _wait_for_slot()
        _LAST_FETCH = _CLOCK()
        try:
            status, headers, body = _fetch_page(page_url(wanted))
        except EzGarageError as exc:
            _remember_error(wanted, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - a page fetch must stay typed
            error = MediafireUnavailable(
                "Mediafire resolve failed (%s)" % type(exc).__name__)
            _remember_error(wanted, error)
            raise error

        if status == 429:
            retry = _retry_after(headers)
            _BACKOFF_UNTIL = max(_BACKOFF_UNTIL, _CLOCK() + retry)
            error = MediafireRateLimited(
                "Mediafire is rate-limiting requests; try again in %d seconds"
                % retry, retry_after=retry)
            _remember_error(wanted, error)
            raise error
        if status in (404, 410):
            error = MediafireUnavailable(
                "that Mediafire set is no longer available", code=404)
            _remember_error(wanted, error)
            raise error
        if status < 200 or status >= 300:
            error = MediafireUnavailable(
                "Mediafire returned HTTP %s" % status)
            _remember_error(wanted, error)
            raise error

        url = signed_url_from_page(body)
        if not url:
            error = MediafireFormatChanged(
                "Mediafire's page no longer contains a direct download link")
            _remember_error(wanted, error)
            raise error

        _remember_url(wanted, url)
        return url
