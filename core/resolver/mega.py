# -*- coding: utf-8 -*-
"""Resolve public MEGA file links to short-lived direct download URLs.

MEGA is not a plain HTTP file host: a share link carries the file handle in the
path and the 32-byte file key in the URL fragment.  The key never reaches the
server in a normal browser request, so the resolver has to:

1. parse ``https://mega.nz/file/<handle>#<key>`` (and the legacy
   ``https://mega.nz/#!<handle>!<key>`` form),
2. ask MEGA's API for an ephemeral download URL, and
3. decrypt the file attributes (the only place the filename lives) to prove
   that the fragment key matches the file.

The download URL returned by MEGA is an opaque, short-lived capability URL.  It
is usually plain ``http`` on a ``*.userstorage.mega.co.nz`` host and may be
bound to the public IP address that requested it.  It is *not* AES-encrypted;
the only encrypted value in the API response is ``at`` (the attributes).  Do
not percent-encode or re-parse the URL before handing it to an HTTP client.

Only MEGA hosts are ever contacted, no proxy is used, and redirects are
refused.  The resolver returns a URL; it is not a download proxy itself.

Live testing (2026-11-09) showed that a storage URL serves roughly two HTTP
requests and then returns HTTP 403, even when the token was unused for 90
seconds.  Treat it as a per-playback capability rather than a reusable public
link: call :func:`invalidate` before a new playback, or fetch it once through
the stream proxy and let the proxy serve client range requests.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import html
import io
import ipaddress
import json
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

try:  # pycryptodome; the only non-stdlib dependency of this module.
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - exercised only in broken deployments
    AES = None


__all__ = [
    "SOURCE",
    "URL_TTL",
    "NEGATIVE_TTL",
    "MegaError",
    "ResolverError",
    "Unsupported",
    "NotFound",
    "RateLimited",
    "FormatChanged",
    "UpstreamError",
    "MegaLink",
    "parse_link",
    "supports",
    "resolve",
    "decrypt_attributes",
    "derive_content_key",
    "validate_download_url",
    "clear_memory",
    "invalidate",
]


SOURCE = "mega"

# MEGA's API endpoint.  The ``id`` query parameter is a cache-buster/sequence
# number; the API accepts any increasing integer.  Do not make this configurable
# from request data: it is the SSRF boundary.
API_ROOT = "https://g.api.mega.co.nz/cs"

# Link hosts we accept.  MEGA has used several official domains; anything else
# (including lookalikes such as ``mega.nz.example.com``) is rejected before any
# network I/O.  The legacy ``mega.co.nz`` domain is still embedded in old posts.
LINK_HOSTS = frozenset((
    "mega.nz",
    "www.mega.nz",
    "mega.co.nz",
    "www.mega.co.nz",
    "mega.io",
    "www.mega.io",
))

# Download URLs are only accepted on MEGA's storage endpoints.  The real host
# looks like ``gfs208n127.userstorage.mega.co.nz``; the API occasionally hands
# back a ``g.api.mega.co.nz`` URL for older accounts.  Exact hosts are listed
# separately from suffixes so ``evil-userstorage.mega.co.nz`` is not accepted by
# accident.
DOWNLOAD_HOSTS = frozenset((
    "g.api.mega.co.nz",
    "g.api.mega.nz",
    "userstorage.mega.co.nz",
    "userstorage.mega.nz",
    "userstorage.mega.io",
))
DOWNLOAD_HOST_SUFFIXES = (
    ".userstorage.mega.co.nz",
    ".userstorage.mega.nz",
    ".userstorage.mega.io",
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Signed storage URLs are deliberately short-lived.  20 minutes is comfortably
# inside MEGA's usual window while still collapsing repeated play requests.
URL_TTL = 20 * 60
NEGATIVE_TTL = 2 * 60
RATE_LIMIT_TTL = 60
MAX_NEGATIVE_TTL = 5 * 60

# A single logical resolve is bounded end-to-end.  The per-request timeout is
# reduced when the remaining budget is smaller, and backoff sleeps count toward
# the same deadline.
DEADLINE = 15.0
REQUEST_TIMEOUT = 8.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5
MAX_BACKOFF = 5.0
FLIGHT_WAIT = DEADLINE + 5.0
MAX_RESPONSE_BYTES = 64 * 1024

_FILE_PATH_RE = re.compile(r"^/file/([A-Za-z0-9_-]{6,16})/?$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{6,16}$")


class MegaError(Exception):
    """Base class for every error this adapter raises.

    ``code`` mirrors an HTTP status so callers can map failures without a large
    isinstance() ladder.  ``negative_cache`` marks failures that are safe to
    remember briefly; a wrong/expired link should not hammer MEGA on every
    play attempt.
    """

    code = 502
    kind = "mega_error"
    negative_cache = False

    def __init__(self, message, code=None, retry_after=None):
        super(MegaError, self).__init__(str(message))
        self.message = str(message)
        if code is not None:
            self.code = int(code)
        self.retry_after = None if retry_after is None else max(0, int(retry_after))

    def __str__(self):
        return self.message


# Nice alias for callers that want a provider-neutral base name.  The classes
# are the same objects, so ``except ResolverError`` catches all of them.
ResolverError = MegaError


class Unsupported(MegaError):
    """The URL is not a MEGA *file* link (folder link, wrong host, bad key)."""

    code = 400
    kind = "unsupported"


class NotFound(MegaError):
    """MEGA says the file does not exist, expired, or is access-blocked."""

    code = 404
    kind = "not_found"
    negative_cache = True


class RateLimited(MegaError):
    """MEGA is throttling us (HTTP 509, EAGAIN, quota, too many connections)."""

    code = 429
    kind = "rate_limited"
    negative_cache = True

    def __init__(self, message, code=None, retry_after=None, retryable=True):
        super(RateLimited, self).__init__(message, code=code, retry_after=retry_after)
        self.retryable = bool(retryable)


class FormatChanged(MegaError):
    """MEGA's response, key, or attribute ciphertext is not in the known shape."""

    code = 502
    kind = "format_changed"


class UpstreamError(MegaError):
    """Network failure, timeout, HTTP 5xx, or an internal MEGA error."""

    code = 502
    kind = "upstream_error"


@dataclass(frozen=True)
class MegaLink:
    """A parsed MEGA file link.

    ``key`` is the canonical unpadded base64url form, so the two URL syntaxes
    and keys with/without ``=`` padding share one cache entry.
    """

    handle: str
    key: str
    key_bytes: bytes = field(repr=False)

    @property
    def canonical(self):
        return "https://mega.nz/file/%s#%s" % (self.handle, self.key)

    def cache_key(self):
        return "mega:%s:%s" % (self.handle, self.key)

    def __str__(self):
        return self.canonical


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #

def _has_control_chars(text):
    return any(ord(char) < 0x20 or ord(char) == 0x7f for char in text)


def _b64decode(value, error_cls, message):
    """Strict base64/base64url decode, accepting missing ``=`` padding."""
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("ascii")
        except UnicodeDecodeError:
            raise error_cls(message)
    else:
        text = str(value or "")
    text = text.strip()
    if not text:
        raise error_cls(message)
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise error_cls(message)


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _coerce_key(value):
    """Return the 32 raw key bytes from a MegaLink, bytes, or base64url string."""
    if isinstance(value, MegaLink):
        return value.key_bytes
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = _b64decode(value, Unsupported, "invalid MEGA file key")
    if len(raw) != 32:
        raise Unsupported("MEGA file keys are 32 bytes (got %d)" % len(raw))
    return raw


def parse_link(value):
    """Parse a MEGA public file link.

    Accepted forms (``http``, ``www.`` and the ``mega.co.nz`` legacy domain are
    tolerated)::

        https://mega.nz/file/<handle>#<key>
        https://mega.nz/#!<handle>!<key>

    Folder links raise :class:`Unsupported` explicitly, before any network I/O.
    Anything that is not a MEGA file link also raises :class:`Unsupported` so a
    caller can fall back to another resolver.
    """
    if isinstance(value, MegaLink):
        return value
    if not isinstance(value, str):
        raise Unsupported("not a MEGA file link")

    text = value.strip()
    if not text or _has_control_chars(text):
        raise Unsupported("not a MEGA file link")
    if "://" not in text:
        # Be forgiving when a database row omits the scheme, but only for an
        # exact official host followed by a slash.
        lowered = text.lower()
        if lowered.startswith(("mega.nz/", "www.mega.nz/", "mega.co.nz/",
                               "www.mega.co.nz/", "mega.io/", "www.mega.io/")):
            text = "https://" + text
        else:
            raise Unsupported("not a MEGA file link")

    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        raise Unsupported("not a MEGA file link")

    if (parts.scheme or "").lower() not in ("http", "https"):
        raise Unsupported("MEGA links must use http(s)")
    if parts.username is not None or parts.password is not None:
        raise Unsupported("MEGA links must not contain credentials")
    try:
        port = parts.port
    except ValueError:
        raise Unsupported("MEGA link has an invalid port")
    if port not in (None, 80, 443):
        raise Unsupported("MEGA link has an unexpected port")

    host = (parts.hostname or "").lower().rstrip(".")
    if host not in LINK_HOSTS:
        raise Unsupported("not a mega.nz link")

    path = parts.path or ""
    fragment = parts.fragment or ""

    # Folder links are valid MEGA links but this adapter only handles files.
    if path == "/folder" or path.startswith("/folder/"):
        raise Unsupported("MEGA folder links are not supported")
    if fragment[:2].upper() == "F!":
        raise Unsupported("MEGA folder links are not supported")

    if path.startswith("/file/"):
        match = _FILE_PATH_RE.match(path)
        if not match:
            raise Unsupported("malformed MEGA file path")
        handle = match.group(1)
        key_text = fragment
        if not key_text:
            raise Unsupported("MEGA file link is missing its key")
        if key_text.startswith("!"):
            # Mixing the legacy fragment with the new path form is not a link
            # MEGA itself produces; treat it as unsupported rather than guess.
            raise Unsupported("malformed MEGA file link")
    elif path in ("", "/") and fragment.startswith("!"):
        body = fragment[1:]
        if "!" not in body:
            raise Unsupported("malformed legacy MEGA link")
        handle, key_text = body.split("!", 1)
        if not _HANDLE_RE.match(handle or ""):
            raise Unsupported("malformed MEGA file handle")
    else:
        raise Unsupported("not a MEGA file link")

    try:
        key_text = urllib.parse.unquote(key_text, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise Unsupported("malformed MEGA file key")
    if not key_text:
        raise Unsupported("MEGA file link is missing its key")

    raw_key = _b64decode(key_text, Unsupported, "invalid MEGA file key")
    if len(raw_key) != 32:
        raise Unsupported("MEGA file keys are 32 bytes (got %d)" % len(raw_key))

    return MegaLink(handle=handle, key=_b64url_encode(raw_key), key_bytes=raw_key)


def supports(value):
    """True when :func:`resolve` recognises ``value`` as a MEGA file link."""
    try:
        parse_link(value)
        return True
    except MegaError:
        return False


# --------------------------------------------------------------------------- #
# Cryptography
# --------------------------------------------------------------------------- #

def _attribute_key(raw_key):
    """MEGA file key -> 16-byte AES key (XOR the two 128-bit halves)."""
    first, second = raw_key[:16], raw_key[16:32]
    return bytes(left ^ right for left, right in zip(first, second))


def _aes_cbc_decrypt(key, iv, ciphertext):
    """Raw AES-CBC decryption; MEGA pads attributes with NUL, not PKCS#7."""
    if AES is None:
        raise MegaError(
            "pycryptodome is required to decrypt MEGA attributes",
            code=500,
        )
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-CBC needs a 16-byte key and IV")
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("AES-CBC ciphertext must be a non-empty multiple of 16")
    return AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)


def derive_content_key(key):
    """Return ``(aes_key, ctr_iv)`` used to decrypt the file body.

    MEGA encrypts file content with AES-128-CTR.  The 16-byte IV is the nonce
    (bytes 16..23 of the link key) followed by eight zero bytes.  This adapter
    only needs URLs, but exposing the derivation keeps the downloader and the
    tests on the same documented scheme.
    """
    raw_key = _coerce_key(key)
    return _attribute_key(raw_key), raw_key[16:24] + (b"\x00" * 8)


def decrypt_attributes(at_b64, key):
    """Decrypt and JSON-decode MEGA's ``at`` attribute blob.

    ``at`` is standard base64url.  It decrypts under AES-128-CBC with
    ``attribute_key = key[0:16] XOR key[16:32]`` and an all-zero IV.  The
    plaintext is ``b"MEGA" + JSON`` followed by NUL padding.  A wrong fragment
    key almost always fails the ``MEGA`` magic check; that is reported as
    :class:`FormatChanged` because it can also mean MEGA changed the scheme.

    Returns a dict (normally containing ``n`` = filename and sometimes ``c`` =
    thumbnail handle).  Raises :class:`FormatChanged` on any parse/crypto
    mismatch.
    """
    raw_key = _coerce_key(key)
    ciphertext = _b64decode(at_b64, FormatChanged, "MEGA attributes are not valid base64")
    try:
        plaintext = _aes_cbc_decrypt(
            _attribute_key(raw_key), b"\x00" * 16, ciphertext)
    except (ValueError, TypeError) as exc:
        raise FormatChanged("could not decrypt MEGA attributes: %s" % exc)
    plaintext = plaintext.rstrip(b"\x00")
    if not plaintext.startswith(b"MEGA"):
        raise FormatChanged(
            "MEGA attribute magic missing (wrong link key or changed format)")
    try:
        attributes = json.loads(plaintext[4:].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FormatChanged("MEGA attributes are not valid JSON")
    if not isinstance(attributes, dict):
        raise FormatChanged("MEGA attributes are not a JSON object")
    return attributes


# --------------------------------------------------------------------------- #
# API transport
# --------------------------------------------------------------------------- #

def _header(headers, name):
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is None:
        try:
            value = headers.get(name.lower())
        except AttributeError:
            value = None
    return str(value or "").strip()


def _retry_after(headers):
    value = _header(headers, "Retry-After")
    if not value:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the API endpoint must never bounce us elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "MEGA API redirect refused: %s" % newurl,
            headers, fp)


def _opener():
    # An empty ProxyHandler deliberately ignores HTTP(S)_PROXY: provider
    # requests must go direct, and an environment proxy is not an open relay we
    # want to hand MEGA credentials/URLs to.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())


def _gunzip_limited(raw):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            data = stream.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, EOFError):
        raise FormatChanged("MEGA API sent invalid gzip")
    if len(data) > MAX_RESPONSE_BYTES:
        raise FormatChanged("MEGA API response is too large")
    return data


def _post_json(url, payload, timeout):
    """POST one JSON request and return ``(status, headers, body_bytes)``.

    Kept as a module-level function so tests (and an embedding service) can
    replace it with an offline transport.  ``timeout`` is a float number of
    seconds and is always bounded by the caller's remaining deadline.
    """
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        response = _opener().open(request, timeout=timeout)
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            headers = response.headers
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        headers = exc.headers
        status = exc.code
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise UpstreamError("MEGA API request failed: %s" % exc)

    if raw is None:
        raw = b""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FormatChanged("MEGA API response is too large")
    if "gzip" in _header(headers, "Content-Encoding").lower():
        raw = _gunzip_limited(raw)
    return int(status), headers, raw


# --------------------------------------------------------------------------- #
# API response handling
# --------------------------------------------------------------------------- #

_API_ERROR_NAMES = {
    -1: "EINTERNAL",
    -2: "EARGS",
    -3: "EAGAIN",
    -4: "ERATELIMIT",
    -5: "EFAILED",
    -6: "ETOOMANY",
    -7: "ERANGE",
    -8: "EEXPIRED",
    -9: "ENOENT",
    -10: "ECIRCULAR",
    -11: "EACCESS",
    -12: "EEXIST",
    -13: "EINCOMPLETE",
    -14: "EKEY",
    -15: "ESID",
    -16: "EBLOCKED",
    -17: "EOVERQUOTA",
    -18: "ETEMPUNAVAIL",
    -19: "ETOOMANYCONNECTIONS",
    -20: "EWRITE",
    -21: "EREAD",
    -22: "EAPPKEY",
    -23: "ESSL",
    -24: "EGOINGOVERQUOTA",
}

_RATE_CODES = frozenset((-3, -4, -6, -17, -18, -19, -24))
_RETRYABLE_RATE_CODES = frozenset((-3, -4, -6, -18, -19))
_NOT_FOUND_CODES = frozenset((-8, -9, -11, -16))
_UPSTREAM_CODES = frozenset((-1, -5, -20, -21))


def _raise_api_error(error, retry_after=None):
    try:
        code = int(error)
    except (TypeError, ValueError):
        raise FormatChanged("MEGA API returned a non-numeric error")
    name = _API_ERROR_NAMES.get(code, "EUNKNOWN")
    message = "MEGA API error %d (%s)" % (code, name)
    if code in _RATE_CODES:
        if code in (-17, -24) and retry_after is None:
            retry_after = 300
        raise RateLimited(
            message, retry_after=retry_after,
            retryable=code in _RETRYABLE_RATE_CODES)
    if code in _NOT_FOUND_CODES:
        raise NotFound(message)
    if code in _UPSTREAM_CODES:
        raise UpstreamError(message)
    # EARGS, EKEY and unknown errors mean our request/response understanding is
    # wrong; retrying the same bytes cannot help.
    raise FormatChanged(message)


def _json_body(raw):
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise FormatChanged("MEGA API response is not UTF-8")
    else:
        raise FormatChanged("MEGA API response has an unsupported type")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise FormatChanged("MEGA API response is not valid JSON")


def _parse_response(status, headers, body):
    """Validate one API reply and return the success object or raise typed errors."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        raise FormatChanged("MEGA API returned an invalid HTTP status")

    retry_after = _retry_after(headers)
    try:
        data = _json_body(body)
    except FormatChanged:
        # An overloaded edge sometimes answers 509 with an HTML page rather
        # than MEGA's usual JSON error.  Classify by HTTP status in that case.
        if status in (509, 429):
            raise RateLimited(
                "MEGA API HTTP %d (rate limited)" % status,
                retry_after=retry_after)
        if status == 404 or status in (400, 403, 410):
            raise NotFound("MEGA API HTTP %d (file unavailable)" % status)
        if status >= 500:
            raise UpstreamError("MEGA API HTTP %d" % status)
        raise

    # A JSON error can arrive with HTTP 200 (MEGA does this for some handles).
    if isinstance(data, list):
        if not data:
            data = None
        else:
            data = data[0]
    if isinstance(data, int):
        data = {"error": data}
    if isinstance(data, dict) and "error" in data:
        _raise_api_error(data.get("error"), retry_after)

    if status in (509, 429):
        raise RateLimited(
            "MEGA API HTTP %d (rate limited)" % status,
            retry_after=retry_after)
    if status == 404:
        raise NotFound("MEGA API HTTP 404 (file not found)")
    if status in (400, 403, 410):
        raise NotFound("MEGA API HTTP %d (file unavailable)" % status)
    if status >= 500:
        raise UpstreamError("MEGA API HTTP %d" % status)
    if status not in (200, 201, 202, 204):
        raise FormatChanged("MEGA API returned unexpected HTTP %d" % status)
    if not isinstance(data, dict):
        raise FormatChanged("MEGA API returned an unexpected response shape")
    return data


# --------------------------------------------------------------------------- #
# Download URL validation
# --------------------------------------------------------------------------- #

def _download_host_allowed(host):
    if host in DOWNLOAD_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in DOWNLOAD_HOST_SUFFIXES)


def validate_download_url(raw):
    """Public wrapper: validate a MEGA storage URL against the host allowlist.

    The stream proxy calls this again immediately before opening a URL, so a
    value that did not come from :func:`resolve` still cannot become an SSRF
    primitive.
    """
    return _validate_download_url(raw)


def _validate_download_url(raw):
    """Return MEGA's storage URL after allowlist/scheme/credential checks.

    The URL is deliberately kept byte-for-byte where possible: its path token
    and query nonce are part of the capability.  We only undo HTML escaping
    (``&amp;``) and a fully percent-encoded URL, both of which occur in JSON
    copied through different clients.
    """
    if not isinstance(raw, str):
        raise FormatChanged("MEGA download URL is not a string")
    text = html.unescape(raw).strip()
    if not text or _has_control_chars(text):
        raise FormatChanged("MEGA download URL is empty or malformed")
    if text.lower().startswith(("http%3a", "https%3a")):
        try:
            text = urllib.parse.unquote(text, errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise FormatChanged("MEGA download URL has invalid percent-encoding")
        if _has_control_chars(text):
            raise FormatChanged("MEGA download URL is malformed")

    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        raise FormatChanged("MEGA download URL cannot be parsed")
    if (parts.scheme or "").lower() not in ("http", "https"):
        raise FormatChanged("MEGA download URL is not http(s)")
    if parts.username is not None or parts.password is not None:
        raise FormatChanged("MEGA download URL contains credentials")
    if parts.fragment:
        raise FormatChanged("MEGA download URL contains a fragment")
    try:
        port = parts.port
    except ValueError:
        raise FormatChanged("MEGA download URL has an invalid port")
    # Storage is plain HTTP/HTTPS only.  Restricting the port keeps an
    # allowlisted host from becoming an egress primitive to SSH/other services.
    if port is not None and port not in (80, 443):
        raise FormatChanged("MEGA download URL has an unexpected port")

    host = (parts.hostname or "").lower()
    if host.endswith("."):
        # Canonicalise a trailing dot before matching the suffix allowlist so
        # it cannot be used to sidestep a future exact/suffix policy change.
        host = host.rstrip(".")
    if not host:
        raise FormatChanged("MEGA download URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # Allowlisting a DNS suffix cannot match an IP literal, but reject it
        # explicitly so a future suffix change cannot turn this into SSRF.
        raise FormatChanged("MEGA download URL points at an IP literal")
    if not _download_host_allowed(host):
        raise FormatChanged("MEGA download URL host is not allowlisted: %s" % host)
    return text


# --------------------------------------------------------------------------- #
# Cache and single-flight
# --------------------------------------------------------------------------- #

class _CacheEntry(object):
    __slots__ = ("value", "error", "expires")

    def __init__(self, value=None, error=None, expires=0.0):
        self.value = value
        self.error = error
        self.expires = expires


_CACHE = {}
_CACHE_LOCK = threading.RLock()
_FLIGHTS = {}
_FLIGHTS_LOCK = threading.Lock()

# These two indirections exist so tests can freeze/advance time without
# sleeping for a real TTL.
def _clock():
    return time.time()


def _monotonic():
    return time.monotonic()


def _copy_result(result):
    if not isinstance(result, dict):
        return result
    copied = dict(result)
    if isinstance(copied.get("headers"), dict):
        copied["headers"] = dict(copied["headers"])
    return copied


def _clone_error(exc):
    if isinstance(exc, RateLimited):
        return RateLimited(
            exc.message, retry_after=exc.retry_after, retryable=exc.retryable)
    if isinstance(exc, MegaError):
        return type(exc)(exc.message)
    return exc


def _cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if entry.expires <= _monotonic():
            _CACHE.pop(key, None)
            return None
        return entry


def _cache_put_value(key, value):
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(
            value=_copy_result(value),
            expires=_monotonic() + URL_TTL,
        )


def _cache_put_error(key, error):
    if isinstance(error, RateLimited):
        ttl = error.retry_after or RATE_LIMIT_TTL
    else:
        ttl = NEGATIVE_TTL
    ttl = max(1, min(int(ttl), MAX_NEGATIVE_TTL))
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(
            error=_clone_error(error),
            expires=_monotonic() + ttl,
        )


def clear_memory():
    """Drop the URL and negative caches (tests, or after a manual fix)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def invalidate(value):
    """Drop one link's cached URL/error, e.g. after storage returned HTTP 403.

    MEGA storage URLs are two-use capabilities (see the module docstring), so a
    caller that starts a new playback should invalidate the entry before asking
    for a fresh URL.  Returns ``True`` when something was actually removed.
    """
    link = parse_link(value)
    with _CACHE_LOCK:
        return _CACHE.pop(link.cache_key(), None) is not None


class _Flight(object):
    __slots__ = ("event", "value", "error")

    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.error = None


def _single_flight(key, factory):
    """Run ``factory`` once per key and share its result/error with waiters."""
    with _FLIGHTS_LOCK:
        flight = _FLIGHTS.get(key)
        leader = flight is None
        if leader:
            flight = _Flight()
            _FLIGHTS[key] = flight

    if leader:
        try:
            flight.value = factory()
        except BaseException as exc:  # noqa: BLE001 - shared with waiters
            flight.error = exc
        finally:
            # Wake waiters before removing the entry; a caller arriving in this
            # tiny window can still join the completed flight instead of
            # starting a duplicate API request.
            flight.event.set()
            with _FLIGHTS_LOCK:
                _FLIGHTS.pop(key, None)
        if flight.error is not None:
            raise flight.error
        return flight.value

    if not flight.event.wait(FLIGHT_WAIT):
        raise UpstreamError("timed out waiting for another MEGA resolve")
    if flight.error is not None:
        raise _clone_error(flight.error)
    return flight.value


# --------------------------------------------------------------------------- #
# Resolve
# --------------------------------------------------------------------------- #

def _request_id():
    # MEGA only needs a monotonically-ish sequence number; randomness avoids
    # collisions between workers sharing an egress IP.
    return random.SystemRandom().randrange(1, 2 ** 31)


def _request_once(link, transport, timeout, now):
    api_url = "%s?id=%d" % (API_ROOT, _request_id())
    payload = [{"a": "g", "g": 1, "p": link.handle}]
    sender = transport or _post_json
    status, headers, body = sender(api_url, payload, timeout)
    data = _parse_response(status, headers, body)

    download_url = _validate_download_url(data.get("g"))
    attributes = decrypt_attributes(data.get("at"), link)
    if "n" in attributes and not isinstance(attributes["n"], str):
        raise FormatChanged("MEGA filename attribute is not a string")
    if "s" in data and not isinstance(data["s"], int):
        raise FormatChanged("MEGA file size is not an integer")

    return {
        "url": download_url,
        "headers": {"User-Agent": USER_AGENT},
        "expires": int(now + URL_TTL),
        "source": SOURCE,
        # Metadata used by the stream proxy to set Content-Type and
        # Content-Length.  Both are optional in MEGA's reply.
        "name": attributes.get("n"),
        "size": data.get("s"),
    }


def _resolve_with_retries(link, transport, sleep, now):
    deadline = _monotonic() + DEADLINE
    last_rate_limit = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        try:
            return _request_once(
                link, transport,
                timeout=max(0.1, min(REQUEST_TIMEOUT, remaining)),
                now=now,
            )
        except RateLimited as exc:
            last_rate_limit = exc
            if not exc.retryable or attempt >= MAX_ATTEMPTS:
                break
            delay = BACKOFF_BASE * (2 ** (attempt - 1))
            if exc.retry_after:
                delay = max(delay, min(float(exc.retry_after), MAX_BACKOFF))
            if _monotonic() + delay >= deadline:
                break
            (sleep or time.sleep)(delay)

    if last_rate_limit is not None:
        raise last_rate_limit
    raise UpstreamError("MEGA API deadline exceeded")


def resolve(value, transport=None, sleep=None, now=None):
    """Resolve a MEGA file link to a direct URL.

    Parameters
    ----------
    value:
        A link string, or an already-parsed :class:`MegaLink`.
    transport:
        Optional offline test hook with the signature
        ``transport(api_url, payload, timeout) -> (status, headers, body)``.
        Defaults to the real HTTPS JSON client.
    sleep:
        Optional sleep hook used between retries (tests pass ``lambda _: None``).
    now:
        Optional wall-clock override for the returned ``expires`` timestamp.

    Returns
    -------
    dict
        ``{"url", "headers", "expires", "source", "name", "size"}``.
        ``name``/``size`` are decrypted metadata for the stream proxy and may
        be ``None``.  ``expires`` is an absolute POSIX timestamp; the URL
        itself is only cached for :data:`URL_TTL` seconds.

    Raises
    ------
    Unsupported, NotFound, RateLimited, FormatChanged, UpstreamError
    """
    link = parse_link(value)
    cache_key = link.cache_key()

    entry = _cache_get(cache_key)
    if entry is not None:
        if entry.error is not None:
            raise _clone_error(entry.error)
        return _copy_result(entry.value)

    def factory():
        # A previous resolver may have filled the cache between the miss above
        # and this flight becoming the leader.
        cached = _cache_get(cache_key)
        if cached is not None:
            if cached.error is not None:
                raise _clone_error(cached.error)
            return _copy_result(cached.value)
        try:
            result = _resolve_with_retries(
                link, transport, sleep,
                _clock() if now is None else float(now),
            )
        except MegaError as exc:
            if exc.negative_cache:
                _cache_put_error(cache_key, exc)
            raise
        _cache_put_value(cache_key, result)
        return result

    return _copy_result(_single_flight(cache_key, factory))


# ---------------------------------------------------------------------------
# Service adapter (core.resolver.service auto-loads MegaResolver)

try:  # the resolver service base is optional for standalone use
    from . import base as _base
except ImportError:  # pragma: no cover - standalone module use
    _base = None

_MegaError = MegaError


def _map_error(exc):
    """Map Mega's typed errors onto the resolver service contract."""
    if _base is None:
        return exc
    name = type(exc).__name__
    if name == "Unsupported":
        return _base.Unsupported(str(exc))
    if name == "NotFound":
        return _base.NotFound(str(exc))
    if name == "RateLimited":
        return _base.RateLimited(str(exc), retry_after=getattr(exc, "retry_after", None))
    if name == "FormatChanged":
        return _base.UpstreamError(str(exc))
    return _base.UpstreamError(str(exc))

class MegaResolver(object):
    """Adapter glue: Mega capability link -> Resolved."""

    source = "mega"

    def __init__(self, timeout=None, http=None, dns=None, clock=None):
        self.timeout = timeout

    def matches(self, url):
        try:
            return bool(supports(url))
        except Exception:
            return False

    def resolve(self, url):
        try:
            data = resolve(url)
        except _MegaError as exc:
            raise _map_error(exc)
        try:
            from .base import Resolved
        except ImportError:  # standalone use
            return data
        try:
            expires = float(data.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        return Resolved(data["url"], data.get("headers") or {},
                        expires, "mega")
