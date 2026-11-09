# -*- coding: utf-8 -*-
"""Decrypting stream proxy for MEGA-hosted audio.

MEGA's storage URL is only half the job: the body is AES-128-CTR ciphertext and
the URL is a short-lived, roughly two-use capability.  This module is the
server-side bridge that makes a MEGA file playable by a normal media player:

1. parse and allowlist the public ``mega.nz/file/<handle>#<key>`` link;
2. resolve it **once** through :mod:`core.resolver.mega` (one API call);
3. open MEGA's storage URL **once** and decrypt the byte stream with AES-CTR;
4. serve plaintext with ``Accept-Ranges: bytes``, mapping a client byte range
   onto the same ciphertext byte range.  AES-CTR is a stream cipher, so
   plaintext offset ``N`` is ciphertext offset ``N``; only the counter block is
   selected (``N // 16``) and the in-block prefix is discarded.

The endpoint is deliberately not an open proxy:

* only links owned by :func:`mega.parse_link` are accepted (Mega hosts only);
* the only URL ever opened is the allowlisted storage URL returned by the
  resolver, and :func:`mega.validate_download_url` checks it again;
* no redirect is followed, no caller-supplied header other than ``Range`` is
  forwarded, and no response header other than the allowlisted media metadata
  is copied;
* the resolver cache and the storage connection are never reused for a
  different link.

``POST /api/mega/sign`` mints a short-lived opaque token for a link so a
player (Kodi, VLC, a TV) can fetch the stream without a browser session
cookie.  The canonical link (including the 32-byte file key) is encrypted into
the token with AES-GCM, so the signed URL contains only an expiry and
ciphertext — never the permanent MEGA capability.  The token is stateless and
reusable until expiry, valid for one canonical link, and deliberately has no
revocation (documented in ``docs/MEGA-STREAM.md``).  Links without a valid
token still work with the normal web session cookie, exactly like every other
``/api/*`` route.

The full HTTP contract, Kodi usage and the LAN/Tailscale caveat are documented
in ``docs/MEGA-STREAM.md``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass

from flask import (Blueprint, Response, current_app, jsonify, request,
                   session, stream_with_context)

from . import mega
from . import stream_budget
from .service import TokenBucket

LOG = logging.getLogger("raveweb.mega_stream")

# --------------------------------------------------------------------------- defaults

CHUNK_SIZE = 64 * 1024
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
STREAM_DEADLINE = 4 * 3600
MAX_STREAM_BYTES = 8 * 1024 * 1024 * 1024
MAX_STREAMS = 4
MAX_URL = 4096
MAX_TOKEN_LEN = 1024

# Signed links are for a player that cannot hold a browser session.  Two hours
# is long enough for a set with pauses and short enough that a URL copied out of
# a log/history stops working quickly; the 7-day cap remains for deliberate
# offline use.  Tokens are stateless, so there is no per-token revocation.
TOKEN_TTL = 2 * 3600
MAX_TOKEN_TTL = 7 * 24 * 3600
TOKEN_VERSION = "v2"

# Request-rate budgets.  ``TokenBucket`` is per process and keyed by identity
# (signed-in) or client IP, so one noisy player cannot consume MEGA's API quota
# for every user on the box.  Set a rate to 0 to disable that bucket.
SIGN_RATE_PER_MINUTE = 30.0
SIGN_BURST = 10
STREAM_RATE_PER_MINUTE = 600.0
STREAM_BURST = 60
RERESOLVE_RATE_PER_MINUTE = 6.0
RERESOLVE_BURST = 2

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}

_RANGE_RE = re.compile(r"^bytes=(\d{0,18})-(\d{0,18})$", re.IGNORECASE)
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$",
                             re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# ``ipaddress.is_global`` is false for these on modern Python, but fec0::/10
# (IPv6 site-local) is still reported as global on 3.9, and the CGNAT block is
# an explicit policy rejection rather than an implementation detail.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")


def _env_number(name, default, minimum, maximum, cast=int):
    try:
        value = cast(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


CONNECT_TIMEOUT = _env_number("RAVEWEB_MEGA_CONNECT_SECONDS", CONNECT_TIMEOUT,
                             1, 60, float)
READ_TIMEOUT = _env_number("RAVEWEB_MEGA_READ_SECONDS", READ_TIMEOUT,
                           5, 300, float)
STREAM_DEADLINE = _env_number("RAVEWEB_MEGA_STREAM_SECONDS", STREAM_DEADLINE,
                              30, 24 * 3600)
MAX_STREAM_BYTES = _env_number("RAVEWEB_MEGA_STREAM_BYTES", MAX_STREAM_BYTES,
                               1 << 20, 1 << 40)
MAX_STREAMS = _env_number("RAVEWEB_MEGA_STREAMS", MAX_STREAMS, 1, 64)
TOKEN_TTL = _env_number("RAVEWEB_MEGA_TOKEN_SECONDS", TOKEN_TTL,
                        60, MAX_TOKEN_TTL)
SIGN_RATE_PER_MINUTE = _env_number(
    "RAVEWEB_MEGA_SIGN_RATE_PER_MINUTE", SIGN_RATE_PER_MINUTE, 0, 60000, float)
SIGN_BURST = _env_number("RAVEWEB_MEGA_SIGN_BURST", SIGN_BURST, 1, 1000)
STREAM_RATE_PER_MINUTE = _env_number(
    "RAVEWEB_MEGA_STREAM_RATE_PER_MINUTE", STREAM_RATE_PER_MINUTE, 0, 60000, float)
STREAM_BURST = _env_number("RAVEWEB_MEGA_STREAM_BURST", STREAM_BURST, 1, 1000)
RERESOLVE_RATE_PER_MINUTE = _env_number(
    "RAVEWEB_MEGA_RERESOLVE_PER_MINUTE", RERESOLVE_RATE_PER_MINUTE, 0, 600, float)
RERESOLVE_BURST = _env_number("RAVEWEB_MEGA_RERESOLVE_BURST", RERESOLVE_BURST,
                              1, 100)

_SLOTS = threading.BoundedSemaphore(MAX_STREAMS)
_SLOT_LOCK = threading.Lock()
_SIGN_LIMITER = TokenBucket(rate_per_minute=SIGN_RATE_PER_MINUTE,
                            burst=SIGN_BURST)
_STREAM_LIMITER = TokenBucket(rate_per_minute=STREAM_RATE_PER_MINUTE,
                              burst=STREAM_BURST)
_RERESOLVE_LIMITER = TokenBucket(rate_per_minute=RERESOLVE_RATE_PER_MINUTE,
                                 burst=RERESOLVE_BURST)


def reset_slots():
    """Replace the stream semaphore(s) (tests, or after a config reload)."""
    global _SLOTS
    with _SLOT_LOCK:
        _SLOTS = threading.BoundedSemaphore(MAX_STREAMS)
    stream_budget.reset_slots()


def reset_limiters():
    """Replace the request/refresh token buckets (tests or a config reload)."""
    global _SIGN_LIMITER, _STREAM_LIMITER, _RERESOLVE_LIMITER
    _SIGN_LIMITER = TokenBucket(rate_per_minute=SIGN_RATE_PER_MINUTE,
                                burst=SIGN_BURST)
    _STREAM_LIMITER = TokenBucket(rate_per_minute=STREAM_RATE_PER_MINUTE,
                                  burst=STREAM_BURST)
    _RERESOLVE_LIMITER = TokenBucket(rate_per_minute=RERESOLVE_RATE_PER_MINUTE,
                                     burst=RERESOLVE_BURST)


# --------------------------------------------------------------------------- errors


class MegaStreamError(Exception):
    """A stream-proxy failure with an HTTP status and a stable JSON code."""

    code = 502
    kind = "upstream"

    def __init__(self, message, code=None, kind=None, retry_after=None,
                 headers=None):
        super(MegaStreamError, self).__init__(str(message))
        self.message = str(message)
        if code is not None:
            self.code = int(code)
        if kind is not None:
            self.kind = kind
        self.retry_after = None if retry_after is None else max(0, int(retry_after))
        self.headers = dict(headers or {})


class RangeNotSatisfiable(MegaStreamError):
    code = 416
    kind = "range_not_satisfiable"

    def __init__(self, total=None):
        headers = {}
        if total is not None:
            headers["Content-Range"] = "bytes */%d" % int(total)
        super(RangeNotSatisfiable, self).__init__(
            "requested range is not satisfiable", code=416,
            kind="range_not_satisfiable", headers=headers)


# --------------------------------------------------------------------------- range parsing


@dataclass(frozen=True)
class RangeSpec:
    """One client byte range, before it is bounded by the file size."""

    start: int = 0
    end: object = None          # inclusive, None means "to the end"
    suffix: int = 0             # bytes from the end (``bytes=-N``)


def parse_range_header(value):
    """Parse a single RFC 7233 byte range; return ``None`` for no range.

    Multiple ranges are rejected (a media player only needs one, and multipart
    responses would triple the decryption/bookkeeping code).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # RFC 7233: a server MUST ignore a Range header whose unit it does not
    # understand.  Only ``bytes`` is implemented here.
    if not text.lower().startswith("bytes="):
        return None
    match = _RANGE_RE.match(text)
    if not match:
        raise RangeNotSatisfiable()
    first, last = match.group(1), match.group(2)
    if not first and not last:
        raise RangeNotSatisfiable()
    if not first:
        # bytes=-N : the final N bytes.
        try:
            suffix = int(last)
        except ValueError:
            raise RangeNotSatisfiable()
        if suffix <= 0:
            raise RangeNotSatisfiable()
        return RangeSpec(suffix=suffix)
    try:
        start = int(first)
        end = int(last) if last else None
    except ValueError:
        raise RangeNotSatisfiable()
    if end is not None and end < start:
        raise RangeNotSatisfiable()
    return RangeSpec(start=start, end=end)


def bounded_range(spec, total):
    """Return ``(start, end)`` inclusive for ``spec``, or raise 416.

    ``end`` is ``None`` when the response is open-ended and the total size is
    unknown.  ``total`` may be ``None``; only suffix ranges require it.
    """
    if spec is None:
        return 0, (total - 1 if total is not None else None)
    if spec.suffix:
        if total is None:
            raise RangeNotSatisfiable()
        if total <= 0:
            raise RangeNotSatisfiable(total)
        length = min(spec.suffix, total)
        return total - length, total - 1
    if total is not None:
        if spec.start >= total:
            raise RangeNotSatisfiable(total)
        end = total - 1 if spec.end is None else min(spec.end, total - 1)
        return spec.start, end
    return spec.start, spec.end


# --------------------------------------------------------------------------- metadata


def content_type_for(name):
    """Map the decrypted MEGA filename (extension) to a media Content-Type."""
    if not isinstance(name, str):
        return "application/octet-stream"
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    extension = base[dot:].lower() if dot >= 0 else ""
    return CONTENT_TYPES.get(extension, "application/octet-stream")


def _clean_name(name):
    if not isinstance(name, str):
        return ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CTRL_RE.sub("", name).strip()
    return name[:255]


def _content_disposition(name):
    """A conservative ``inline`` filename, ASCII fallback + RFC 5987 UTF-8."""
    name = _clean_name(name)
    if not name:
        return ""
    fallback = name.encode("ascii", "replace").decode("ascii")
    fallback = fallback.replace('"', "'")
    return "inline; filename=\"%s\"; filename*=UTF-8''%s" % (
        fallback, urllib.parse.quote(name, safe=""))


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _header(resp, name):
    try:
        return resp.getheader(name)
    except AttributeError:
        return None


# --------------------------------------------------------------------------- signed tokens


def _token_key(secret=None):
    """Return the token master key, or ``None`` when signing is unconfigured.

    ``RAVEWEB_MEGA_TOKEN_SECRET`` is preferred so rotating the Flask session
    secret does not silently invalidate every outstanding media URL.  Otherwise
    a dedicated subkey of the Flask secret is derived; the raw session secret
    is never used as a token key directly.
    """
    if secret is not None:
        if isinstance(secret, bytes):
            return secret
        return str(secret).encode("utf-8")
    configured = (os.environ.get("RAVEWEB_MEGA_TOKEN_SECRET") or "").strip()
    if configured:
        return configured.encode("utf-8")
    try:
        flask_secret = current_app.secret_key
    except RuntimeError:  # outside an application context
        flask_secret = None
    if not flask_secret:
        return None
    raw = flask_secret.encode("utf-8") if isinstance(flask_secret, str) else flask_secret
    return hmac.new(raw, b"raveplayer-mega-stream-token-v1",
                    hashlib.sha256).digest()


def _token_cipher_key(secret=None):
    """Derive a dedicated AES-256-GCM key from the token master secret."""
    key = _token_key(secret)
    if key is None:
        return None
    return hashlib.sha256(
        key + b"raveplayer-mega-stream-gcm-v2").digest()


def _canonical_link(link):
    try:
        return mega.parse_link(link).canonical
    except mega.MegaError:
        raise MegaStreamError("not a mega.nz file link", code=400,
                              kind="unsupported")


def _token_aad(expires):
    return ("%s\n%d" % (TOKEN_VERSION, int(expires))).encode("ascii")


def make_token(link, expires, secret=None):
    """Return an opaque, URL-safe token for ``link`` valid until ``expires``.

    The canonical link (handle **and** 32-byte key) is encrypted with
    AES-256-GCM and authenticated together with the expiry.  The signed URL
    therefore carries ciphertext, not the permanent MEGA capability.
    """
    if mega.AES is None:
        raise MegaStreamError(
            "signed MEGA links are not configured", code=503,
            kind="not_configured")
    key = _token_cipher_key(secret)
    if key is None:
        raise MegaStreamError(
            "signed MEGA links are not configured", code=503,
            kind="not_configured")
    canonical = _canonical_link(link)
    nonce = os.urandom(12)
    cipher = mega.AES.new(key, mega.AES.MODE_GCM, nonce=nonce)
    cipher.update(_token_aad(expires))
    ciphertext, tag = cipher.encrypt_and_digest(canonical.encode("utf-8"))
    raw = nonce + ciphertext + tag
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_token(expires, token, secret=None):
    """Decrypt a v2 token, returning its canonical link or ``None``."""
    if mega.AES is None:
        return None
    if not isinstance(token, str) or not token or not token.isascii():
        return None
    if len(token) > MAX_TOKEN_LEN:
        return None
    key = _token_cipher_key(secret)
    if key is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(
            token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) < 12 + 16 + 1:
        return None
    nonce, ciphertext, tag = raw[:12], raw[12:-16], raw[-16:]
    try:
        cipher = mega.AES.new(key, mega.AES.MODE_GCM, nonce=nonce)
        cipher.update(_token_aad(expires))
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _token_expiry_valid(expires, now=None):
    try:
        expiry = int(expires)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else float(now)
    if expiry <= current or expiry > current + MAX_TOKEN_TTL:
        return None
    return expiry


def verify_token(link, expires, token, secret=None, now=None):
    """Constant-time validation of an opaque token for one canonical link."""
    expiry = _token_expiry_valid(expires, now=now)
    if expiry is None:
        return False
    decoded = _decode_token(expiry, token, secret=secret)
    if decoded is None:
        return False
    try:
        canonical = _canonical_link(link)
    except MegaStreamError:
        return False
    return hmac.compare_digest(decoded, canonical)


def signed_path(link, ttl=None, now=None, secret=None):
    """Build the relative ``/api/mega/stream?...`` path for a signed link.

    Only the expiry and the authenticated ciphertext appear in the query
    string; the MEGA link and its file key never do.
    """
    canonical = _canonical_link(link)
    lifetime = TOKEN_TTL if ttl is None else max(1, min(int(ttl), MAX_TOKEN_TTL))
    expires = int((time.time() if now is None else float(now)) + lifetime)
    token = make_token(canonical, expires, secret=secret)
    query = urllib.parse.urlencode({"e": str(expires), "t": token})
    return "/api/mega/stream?" + query


def token_link(args, secret=None, now=None):
    """Return the canonical link carried by a valid signed request, or None.

    A legacy-style ``u`` parameter is accepted only when it matches the
    decrypted token; it cannot add a second capability.  Never raises.
    """
    if args is None:
        return None
    try:
        expires = args.get("e")
        token = args.get("t")
        if not expires or not token:
            return None
        expiry = _token_expiry_valid(expires, now=now)
        if expiry is None:
            return None
        decoded = _decode_token(expiry, token, secret=secret)
        if decoded is None:
            return None
        raw_link = args.get("u")
        if raw_link:
            if not isinstance(raw_link, str) or len(raw_link) > MAX_URL:
                return None
            canonical = _canonical_link(raw_link)
            if not hmac.compare_digest(decoded, canonical):
                return None
        return decoded
    except Exception:  # malformed query data must never 500 the auth guard
        return None


def has_valid_token(args):
    """True when a request carries a valid opaque stream token.  Never raises."""
    return token_link(args) is not None


def _session_authorized():
    """Match the web app's session guard, with an injectable test fallback."""
    try:
        checker = current_app.config.get("MEGA_STREAM_AUTH_CHECK")
    except RuntimeError:
        checker = None
    if checker is not None:
        try:
            return bool(checker() if callable(checker) else checker)
        except Exception:
            return False
    try:
        import app as app_module  # local import: avoid an import cycle at boot
    except Exception:
        app_module = None
    validator = getattr(app_module, "_session_valid", None)
    if callable(validator):
        try:
            return bool(validator())
        except Exception:
            return False
    try:
        from flask import session
        return bool(session.get("auth") and session.get("sid"))
    except Exception:
        return False


def authorized(args=None):
    """A request may stream when it has a valid signed token or a session."""
    return has_valid_token(args) or _session_authorized()


# --------------------------------------------------------------------------- storage transport


class StorageResponse(object):
    """Thin wrapper over ``http.client`` so tests can inject a fake."""

    __slots__ = ("_conn", "_resp")

    def __init__(self, conn, resp):
        self._conn = conn
        self._resp = resp

    @property
    def status(self):
        return int(getattr(self._resp, "status", 0) or 0)

    def getheader(self, name):
        return self._resp.getheader(name)

    def read1(self, size):
        return self._resp.read1(size)

    def close(self):
        try:
            self._resp.close()
        finally:
            try:
                self._conn.close()
            except Exception:
                pass


_SSL_CONTEXT = ssl.create_default_context()


def _public_address(host, port):
    """Resolve ``host`` and return one public IP; reject private/mixed answers.

    The URL is already restricted to MEGA's storage suffixes, but pinning the
    connection to a validated address means a poisoned DNS answer cannot make
    the proxy open a private socket.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise MegaStreamError("MEGA storage host could not be resolved")
    if not infos:
        raise MegaStreamError("MEGA storage host could not be resolved")
    addresses = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise MegaStreamError("MEGA storage host returned an unusable address")
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        # Positive policy: only addresses the platform considers globally
        # routable are accepted.  The explicit CGNAT/site-local checks keep the
        # behaviour stable across Python versions and document the rejection.
        if (not address.is_global
                or getattr(address, "is_site_local", False)
                or address.is_multicast
                or address.is_unspecified
                or address.is_loopback
                or address.is_link_local
                or address.is_private
                or address.is_reserved
                or (address.version == 4 and address in _CGNAT)
                or (address.version == 6 and address in _SITE_LOCAL_V6)):
            raise MegaStreamError("MEGA storage host resolved to a non-public address")
        addresses.append(address)
    chosen = next((address for address in addresses if address.version == 4),
                  addresses[0])
    return str(chosen)


def _request_target(url):
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return path


def open_storage(url, headers, timeout=None):
    """Open the allowlisted MEGA storage URL exactly once (no redirects)."""
    try:
        checked = mega.validate_download_url(url)
    except mega.MegaError:
        raise MegaStreamError("MEGA returned an unsafe storage URL")
    parts = urllib.parse.urlsplit(checked)
    host = (parts.hostname or "").lower().rstrip(".")
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise MegaStreamError("MEGA storage URL has an invalid port")
    ip = _public_address(host, port)
    if scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port, timeout=CONNECT_TIMEOUT, context=_SSL_CONTEXT)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)

    def create_connection(address, timeout=None, source_address=None):
        return socket.create_connection((ip, port), timeout or CONNECT_TIMEOUT,
                                        source_address)

    conn._create_connection = create_connection
    try:
        conn.request("GET", _request_target(checked), headers=headers)
        resp = conn.getresponse()
    except (socket.timeout, TimeoutError):
        conn.close()
        raise MegaStreamError("MEGA storage timed out", code=504,
                              kind="timeout")
    except (http.client.HTTPException, ssl.SSLError, OSError, ValueError) as exc:
        conn.close()
        LOG.info("MEGA storage connect failed (%s)", type(exc).__name__)
        raise MegaStreamError("MEGA storage connection failed")
    try:
        conn.sock.settimeout(READ_TIMEOUT)
    except (AttributeError, OSError):
        pass
    return StorageResponse(conn, resp)


# --------------------------------------------------------------------------- streamer


class _Once(object):
    """Run a cleanup callback at most once, from any thread."""

    __slots__ = ("_callback", "_lock", "_done")

    def __init__(self, callback):
        self._callback = callback
        self._lock = threading.Lock()
        self._done = False

    def __call__(self):
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            self._callback()
        except Exception:  # cleanup must never mask the response
            pass


class StreamPlan(object):
    """Everything the Flask view needs, plus an idempotent ``close()``."""

    __slots__ = ("status", "headers", "body", "_close")

    def __init__(self, status, headers, body, close):
        self.status = status
        self.headers = headers
        self.body = body
        self._close = close or (lambda: None)

    def close(self):
        self._close()


class MegaStreamer(object):
    """Resolve + decrypt one MEGA file for one client request.

    ``resolve`` and ``opener`` are injection seams used by the offline tests:

    * ``resolve(link) -> {"url", "name", "size", ...}``
    * ``opener(url, headers, timeout) -> StorageResponse``
    """

    def __init__(self, resolve=None, opener=None, clock=time.time,
                 acquire_slot=True, reresolve_limiter=None):
        self._resolve = resolve or mega.resolve
        self._opener = opener or open_storage
        self._clock = clock
        self._acquire_slot = acquire_slot
        self._reresolve_limiter = reresolve_limiter or _RERESOLVE_LIMITER

    # -- public ---------------------------------------------------------

    def plan(self, raw_link, range_header=None, head=False):
        link = mega.parse_link(raw_link)
        try:
            spec = parse_range_header(range_header)
        except RangeNotSatisfiable as exc:
            # Resolve only to fill in ``Content-Range: bytes */<size>`` for a
            # malformed range.  The request-rate bucket already ran, and the
            # resolver cache absorbs repeats.
            self._add_range_total(link, exc)
            raise

        global_held = False
        local_held = False
        resp = None
        # HEAD must not bypass the budget: it still performs a resolve and can
        # be used to enumerate distinct links.  The shared budget is always
        # acquired first, then the narrower MEGA cap, so both routes compose.
        if self._acquire_slot:
            global_held = stream_budget.acquire(timeout=2.0)
            if not global_held:
                raise MegaStreamError(
                    "too many streams in progress, retry shortly",
                    code=503, kind="busy", retry_after=2)
            local_held = _SLOTS.acquire(timeout=2.0)
            if not local_held:
                stream_budget.release()
                raise MegaStreamError(
                    "too many MEGA streams in progress, retry shortly",
                    code=503, kind="busy", retry_after=2)

        def release():
            if local_held:
                try:
                    _SLOTS.release()
                except ValueError:  # pragma: no cover - double release guard
                    pass
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            if global_held:
                stream_budget.release()

        close = _Once(release)
        try:
            resolved = self._resolve(link)
            size = _positive_int(resolved.get("size")) or None
            name = _clean_name(resolved.get("name"))
            try:
                start, end = bounded_range(spec, size)
            except RangeNotSatisfiable as exc:
                if size is not None:
                    exc.headers["Content-Range"] = "bytes */%d" % size
                raise

            ranged = spec is not None
            status = 206 if ranged else 200
            content_type = content_type_for(name)

            if head:
                length = self._length(start, end, size, ranged)
                headers = self._client_headers(
                    content_type, name, length, start, end, size, ranged)
                close()
                return StreamPlan(status, headers, None, close)

            upstream_range = self._upstream_range(spec, start, end)
            block_start = start - (start % 16)
            resp = self._open(self._url(resolved), upstream_range)
            resolved, resp = self._retry_once_on_403(
                link, resolved, resp, upstream_range)

            try:
                upstream_status = self._validate_status(resp)
            except RangeNotSatisfiable as exc:
                if size is not None:
                    exc.headers["Content-Range"] = "bytes */%d" % size
                raise

            if upstream_status == 206:
                server_start = self._content_range_start(resp)
                if server_start is None:
                    # A 206 without a parsable Content-Range is not proof the
                    # body starts at the client's ``start``.  The only range we
                    # actually requested is ``block_start``, so decrypt from
                    # there; never assume the client offset.
                    LOG.info("MEGA 206 without parsable Content-Range; "
                             "using the requested block start")
                    server_start = block_start
                server_end = self._content_range_end(resp)
                if server_end is not None and end is not None:
                    end = min(end, server_end)
            else:
                # The storage node ignored the Range header and sent the
                # whole ciphertext body; skip to the requested offset below.
                server_start = 0
            if server_start is None or server_start > start:
                raise MegaStreamError("MEGA returned an unexpected byte range")

            # Recompute size only when the API did not tell us (a re-resolve
            # may have supplied it the second time round).
            if size is None:
                size = _positive_int(resolved.get("size")) or None
            total = size
            if total is None:
                total = self._content_range_total(resp)
            if total is None and upstream_status == 200:
                total = _positive_int(_header(resp, "Content-Length"))
            if ranged and end is None and total is not None:
                end = total - 1

            discard = start - server_start
            if discard >= MAX_STREAM_BYTES:
                headers = {}
                if total is not None:
                    headers["Content-Range"] = "bytes */%d" % total
                raise MegaStreamError(
                    "requested offset is beyond the per-stream byte budget",
                    code=416, kind="range_not_satisfiable",
                    headers=headers)

            length = self._length(start, end, total, ranged)
            headers = self._client_headers(
                content_type, name, length, start, end, total, ranged)

            cipher = self._new_cipher(link, server_start)
            deadline = self._clock() + STREAM_DEADLINE

            def body():
                try:
                    for chunk in self._pump(resp, cipher, discard, length,
                                            deadline):
                        yield chunk
                finally:
                    close()

            return StreamPlan(status, headers, body(), close)
        except BaseException:
            close()
            raise

    # -- helpers --------------------------------------------------------

    def _add_range_total(self, link, exc):
        """Best-effort ``Content-Range: bytes */<size>`` for a bad Range."""
        try:
            resolved = self._resolve(link)
            size = _positive_int(resolved.get("size")) or None
        except Exception:
            return
        if size is not None:
            exc.headers["Content-Range"] = "bytes */%d" % size

    @staticmethod
    def _length(start, end, total, ranged):
        if not ranged:
            return total
        if end is not None:
            return max(0, end - start + 1)
        if total is not None:
            return max(0, total - start)
        return None

    @staticmethod
    def _client_headers(content_type, name, length, start, end, total, ranged):
        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
        }
        if length is not None:
            headers["Content-Length"] = str(length)
        if ranged and end is not None:
            headers["Content-Range"] = "bytes %d-%d/%s" % (
                start, end, total if total is not None else "*")
        disposition = _content_disposition(name)
        if disposition:
            headers["Content-Disposition"] = disposition
        return headers

    @staticmethod
    def _upstream_range(spec, start, end):
        if spec is None:
            return None
        # Request from the start of the AES block so the keystream lines up;
        # the few extra plaintext bytes are discarded by _pump().
        block_start = start - (start % 16)
        if end is None:
            return "bytes=%d-" % block_start
        return "bytes=%d-%d" % (block_start, end)

    def _open(self, url, upstream_range):
        headers = {
            "User-Agent": mega.USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if upstream_range:
            headers["Range"] = upstream_range
        return self._opener(url, headers, READ_TIMEOUT)

    def _retry_once_on_403(self, link, resolved, resp, upstream_range):
        """MEGA storage URLs are short-lived; on 403 re-resolve exactly once.

        The retry is also budgeted per canonical link so repeated playbacks
        cannot turn a stream endpoint into an unbounded MEGA API amplifier.
        """
        if getattr(resp, "status", 0) != 403:
            return resolved, resp
        try:
            resp.close()
        except Exception:
            pass
        retry = self._reresolve_limiter.take(link.cache_key())
        if retry:
            LOG.warning(
                "MEGA 403 re-resolve budget exhausted for handle %s", link.handle)
            raise MegaStreamError(
                "too many MEGA storage refreshes; retry shortly",
                code=429, kind="rate_limited", retry_after=retry)
        LOG.info("MEGA storage returned 403; invalidating and re-resolving once")
        try:
            mega.invalidate(link)
        except Exception:
            pass
        resolved = self._resolve(link)
        resp = self._open(self._url(resolved), upstream_range)
        if getattr(resp, "status", 0) == 403:
            LOG.info("MEGA storage refused the re-resolved URL (403); giving up")
            try:
                resp.close()
            except Exception:
                pass
            raise MegaStreamError("MEGA refused the storage request", code=403,
                                  kind="upstream_refused")
        return resolved, resp

    @staticmethod
    def _url(resolved):
        url = resolved.get("url") if isinstance(resolved, dict) else None
        if not isinstance(url, str) or not url:
            raise MegaStreamError("MEGA did not return a storage URL")
        return url

    @staticmethod
    def _validate_status(resp):
        status = int(getattr(resp, "status", 0) or 0)
        if status in (200, 206):
            encoding = (_header(resp, "Content-Encoding") or "").strip().lower()
            if encoding not in ("", "identity"):
                try:
                    resp.close()
                except Exception:
                    pass
                raise MegaStreamError(
                    "MEGA storage sent an encoded stream")
            return status
        if status in (404, 410):
            try:
                resp.close()
            except Exception:
                pass
            raise MegaStreamError("MEGA storage says the file is gone", code=404,
                                  kind="not_found")
        if status == 416:
            try:
                resp.close()
            except Exception:
                pass
            raise RangeNotSatisfiable()
        if status in (401, 403):
            try:
                resp.close()
            except Exception:
                pass
            raise MegaStreamError("MEGA refused the storage request", code=403,
                                  kind="upstream_refused")
        if status >= 500:
            try:
                resp.close()
            except Exception:
                pass
            raise MegaStreamError("MEGA storage is unavailable")
        try:
            resp.close()
        except Exception:
            pass
        raise MegaStreamError("MEGA storage returned an unexpected status")

    @staticmethod
    def _content_range_start(resp):
        match = _CONTENT_RANGE_RE.match((_header(resp, "Content-Range") or "").strip())
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _content_range_end(resp):
        match = _CONTENT_RANGE_RE.match((_header(resp, "Content-Range") or "").strip())
        if not match:
            return None
        try:
            return int(match.group(2))
        except ValueError:
            return None

    @staticmethod
    def _content_range_total(resp):
        match = _CONTENT_RANGE_RE.match((_header(resp, "Content-Range") or "").strip())
        if not match or match.group(3) == "*":
            return None
        try:
            return int(match.group(3))
        except ValueError:
            return None

    @staticmethod
    def _new_cipher(link, server_start):
        """AES-CTR positioned at ``server_start`` (128-bit counter block)."""
        if mega.AES is None:
            raise MegaStreamError("pycryptodome is required to decrypt MEGA audio",
                                  code=500, kind="not_configured")
        aes_key, iv = mega.derive_content_key(link)
        block, offset = divmod(int(server_start), 16)
        cipher = mega.AES.new(aes_key, mega.AES.MODE_CTR, nonce=iv[:8],
                              initial_value=block)
        if offset:
            cipher.decrypt(b"\x00" * offset)
        return cipher

    def _pump(self, resp, cipher, discard, length, deadline):
        remaining = length
        # The cap covers *all* upstream bytes read, including the bytes skipped
        # to reach a client offset when the storage node ignored Range.  Without
        # this, a request near the end of a large file could make the proxy
        # decrypt the whole prefix before sending anything.
        cap = MAX_STREAM_BYTES
        consumed = 0
        try:
            while discard > 0:
                if self._clock() >= deadline:
                    return
                if consumed >= cap:
                    LOG.warning("MEGA stream byte cap reached during skip")
                    return
                chunk = resp.read1(min(CHUNK_SIZE, discard, cap - consumed))
                if not chunk:
                    return
                consumed += len(chunk)
                cipher.decrypt(chunk)
                discard -= len(chunk)
            while True:
                if self._clock() >= deadline:
                    LOG.warning("MEGA stream deadline reached")
                    return
                if consumed >= cap:
                    LOG.warning("MEGA stream byte cap reached")
                    return
                try:
                    chunk = resp.read1(min(CHUNK_SIZE, cap - consumed))
                except (socket.timeout, TimeoutError):
                    LOG.info("MEGA storage read timed out")
                    return
                except (http.client.HTTPException, OSError, ValueError):
                    return
                if not chunk:
                    return
                consumed += len(chunk)
                data = cipher.decrypt(chunk)
                if remaining is not None:
                    data = data[:remaining]
                    remaining -= len(data)
                if data:
                    yield data
                if remaining is not None and remaining <= 0:
                    return
        finally:
            try:
                resp.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- Flask API


mega_stream_bp = Blueprint("mega_stream", __name__)


@mega_stream_bp.before_request
def _require_auth():
    # A signed token authenticates streaming only.  In particular it must not
    # be replayable at /api/mega/sign to mint a fresh token and extend access.
    if request.path == "/api/mega/stream":
        allowed = authorized(request.args)
    else:
        allowed = _session_authorized()
    if not allowed:
        return _error_response(MegaStreamError(
            "authentication required", code=401, kind="unauthorized"))


@mega_stream_bp.after_request
def _security_headers(response):
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


def _client_key():
    """Per-identity key when signed in, otherwise the client IP."""
    try:
        if session.get("sid") and session.get("identity"):
            return "id:%s" % session.get("identity")
    except Exception:
        pass
    return "ip:%s" % (request.remote_addr or "unknown")


def _rate_error(limiter, label):
    retry = limiter.take(_client_key())
    if not retry:
        return None
    return MegaStreamError(
        "too many MEGA %s requests; retry shortly" % label,
        code=429, kind="rate_limited", retry_after=retry)


@mega_stream_bp.route("/api/mega/stream", methods=["GET"])
def stream_endpoint():
    limited = _rate_error(_STREAM_LIMITER, "stream")
    if limited is not None:
        return _error_response(limited)
    raw_link = request.args.get("u", "")
    if not raw_link:
        # An opaque signed URL carries the canonical link inside the token;
        # ``u`` is no longer part of the minted query string.
        raw_link = token_link(request.args) or ""
    if not raw_link or len(raw_link) > MAX_URL:
        return _error_response(MegaStreamError(
            "u is required", code=400, kind="invalid"))
    try:
        plan = MegaStreamer().plan(raw_link, request.headers.get("Range"),
                                   head=(request.method == "HEAD"))
    except MegaStreamError as exc:
        return _error_response(exc)
    except mega.Unsupported as exc:
        return _error_response(MegaStreamError(str(exc), code=400,
                                               kind="unsupported"))
    except mega.NotFound as exc:
        return _error_response(MegaStreamError(str(exc), code=404,
                                               kind="not_found"))
    except mega.RateLimited as exc:
        return _error_response(MegaStreamError(
            str(exc), code=429, kind="rate_limited",
            retry_after=exc.retry_after))
    except mega.MegaError as exc:
        return _error_response(MegaStreamError(str(exc)))
    except Exception:
        current_app.logger.exception("MEGA stream: unexpected failure")
        return _error_response(MegaStreamError("MEGA stream failed"))

    if plan.body is None:
        plan.close()
        response = Response(status=plan.status, headers=plan.headers)
        # A HEAD response carries the GET's Content-Length, not 0.
        for name in ("Content-Length", "Content-Range", "Accept-Ranges",
                     "Content-Type", "Content-Disposition"):
            if name in plan.headers:
                response.headers[name] = plan.headers[name]
        return response
    response = Response(stream_with_context(plan.body), status=plan.status,
                        headers=plan.headers, direct_passthrough=True)
    response.call_on_close(plan.close)
    return response


@mega_stream_bp.route("/api/mega/sign", methods=["GET", "POST"])
def sign_endpoint():
    limited = _rate_error(_SIGN_LIMITER, "sign")
    if limited is not None:
        return _error_response(limited)
    raw_link = _sign_request_link()
    if not raw_link or len(raw_link) > MAX_URL:
        return _error_response(MegaStreamError("url is required", code=400,
                                               kind="invalid"))
    try:
        link = mega.parse_link(raw_link)
    except mega.MegaError as exc:
        return _error_response(MegaStreamError(str(exc), code=400,
                                               kind="unsupported"))
    try:
        path = signed_path(link)
    except MegaStreamError as exc:
        return _error_response(exc)
    absolute = request.url_root.rstrip("/") + path
    response = jsonify({
        "ok": True,
        "url": path,
        "path": path,
        "absolute_url": absolute,
        "expires": int(urllib.parse.parse_qs(
            urllib.parse.urlsplit(path).query).get("e", ["0"])[0]),
        "source": mega.SOURCE,
    })
    if request.method == "GET":
        # POST keeps the MEGA capability out of the request line (and therefore
        # out of proxy access logs).  GET still works for older clients, but is
        # visibly deprecated.
        response.headers["Warning"] = (
            '299 - "GET is deprecated; use POST /api/mega/sign"')
        response.headers["Deprecation"] = "true"
        LOG.warning("deprecated GET /api/mega/sign from %s",
                    request.remote_addr or "unknown")
    return response


def _sign_request_link():
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            value = payload.get("url") or payload.get("u")
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = request.form.get("url") or request.form.get("u")
        return value.strip() if isinstance(value, str) else ""
    value = request.args.get("u") or request.args.get("url")
    return value.strip() if isinstance(value, str) else ""


def _error_response(exc):
    payload = {"error": exc.message}
    if exc.kind:
        payload["code"] = exc.kind
    response = jsonify(payload)
    status = int(exc.code or 502)
    response.status_code = status if 400 <= status <= 599 else 502
    if exc.retry_after:
        response.headers["Retry-After"] = str(max(1, int(exc.retry_after)))
    for name, value in exc.headers.items():
        response.headers[name] = value
    return response


def init_app(app):
    if "mega_stream" not in app.blueprints:
        app.register_blueprint(mega_stream_bp)
    return app


__all__ = [
    "MegaStreamError",
    "RangeNotSatisfiable",
    "MegaStreamer",
    "StreamPlan",
    "StorageResponse",
    "content_type_for",
    "parse_range_header",
    "bounded_range",
    "make_token",
    "verify_token",
    "signed_path",
    "token_link",
    "has_valid_token",
    "authorized",
    "open_storage",
    "mega_stream_bp",
    "init_app",
    "reset_slots",
    "reset_limiters",
]
