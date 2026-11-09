# -*- coding: utf-8 -*-
"""Short-lived signed playback URLs for web-proxied Mediafire streams.

``/api/ezgarage/<id>/stream`` and ``/api/ukraves/<id>/stream`` are session-only
in the browser: the page plays them through an ``<audio>`` element, which
automatically carries the login cookie.  A media player (Kodi, VLC, a TV) has
no cookie jar for the URL the browser just showed it, so those two rows answer
``401``.

``GET /api/webplay?path=...`` closes that gap.  A caller that already holds the
narrow ``RAVEWEB_RESOLVER_TOKEN`` bearer mints a signed relative stream URL:

    /api/ezgarage/72aswcrx4hlw2sh/stream?e=1794220000&t=<hex>

The signature is a stateless HMAC-SHA256 over the **exact request path** and
the expiry, with a NUL separator::

    HMAC-SHA256(SECRET_KEY, path + "\\0" + expiry)

The auth guard accepts it only when the request path is itself one of the two
signable stream paths and the expiry is in the future.  A signature minted for
one path therefore cannot be replayed at another path, at ``/api/latest``, at
``/api/ezgarage/<id>/file`` or at ``/api/webplay``.  Tokens are intentional
bearer capabilities until expiry (default one hour); they are never logged by
the app and they carry no permanent Mediafire capability.

The endpoint and the guard intentionally share :func:`verify` /
:func:`has_valid_signature`: there is exactly one signature parser.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import urllib.parse

from flask import current_app

# ``path`` is a same-origin relative path and nothing else.  ``fullmatch`` is
# stricter than ``re.match(...$)``: absolute URLs, protocol-relative URLs,
# query strings, fragments, percent-encoded separators, trailing slashes and
# path traversal never reach the HMAC.
SIGNED_PATH_RE = re.compile(
    r"/api/(?:ezgarage|ukraves)/[A-Za-z0-9_-]+/stream\Z")
MAX_PATH_LEN = 512
MAX_TOKEN_LEN = 128

# One hour is long enough to start playback and seek, short enough that a URL
# copied out of a log/history stops working quickly.  A far-future expiry is
# rejected even when correctly signed, mirroring the MEGA stream token policy.
TOKEN_TTL = 3600
MAX_TOKEN_TTL = 7 * 24 * 3600

_EXPIRY_RE = re.compile(r"[0-9]{1,12}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")


class WebplayError(ValueError):
    """A request that cannot be signed, with the status the route should use."""

    code = 400

    def __init__(self, message, code=None):
        super(WebplayError, self).__init__(str(message))
        self.message = str(message)
        if code is not None:
            self.code = int(code)


def is_signable_path(path):
    """True only for the two exact Mediafire stream path shapes."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_LEN:
        return False
    return SIGNED_PATH_RE.fullmatch(path) is not None


def _secret_bytes(secret=None):
    """Return the HMAC key as bytes, or ``None`` when signing is unavailable.

    ``SECRET_KEY`` is the app's signing key (``app.secret_key``); a caller may
    inject ``secret`` in tests.  Outside an application context the environment
    variable is the fallback.
    """
    if secret is not None:
        return secret if isinstance(secret, bytes) else str(secret).encode("utf-8")
    try:
        value = current_app.secret_key
    except RuntimeError:  # outside an application context
        value = None
    if not value:
        value = (os.environ.get("SECRET_KEY") or "").strip()
    if not value:
        return None
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _message(path, expiry):
    # Sign the exact expiry spelling that will appear in the URL.  A caller
    # cannot change ``e=1800000000`` to ``e=01800000000`` and keep the token
    # valid: the NUL-separated message no longer matches.
    return path.encode("utf-8") + b"\x00" + str(expiry).encode("ascii")


def signature(path, expiry, secret=None):
    """Return the lowercase hex HMAC-SHA256 for ``path`` and ``expiry``.

    Raises :class:`WebplayError` when the path is not signable or the key is
    missing; callers that must not raise use :func:`verify` or
    :func:`has_valid_signature`.
    """
    if not is_signable_path(path):
        raise WebplayError("path is not a signable stream path")
    key = _secret_bytes(secret)
    if key is None:
        raise WebplayError("signed playback URLs are not configured", code=503)
    return hmac.new(key, _message(path, expiry), hashlib.sha256).hexdigest()


def signed_path(path, ttl=None, now=None, secret=None):
    """Build ``path?e=<expiry>&t=<hex>`` for one signable stream path.

    ``ttl`` defaults to :data:`TOKEN_TTL` (one hour) and is clamped to
    :data:`MAX_TOKEN_TTL`.  ``now`` and ``secret`` are test seams.
    """
    if not is_signable_path(path):
        raise WebplayError("path is not a signable stream path")
    lifetime = TOKEN_TTL if ttl is None else max(1, min(int(ttl), MAX_TOKEN_TTL))
    expiry = int((time.time() if now is None else float(now)) + lifetime)
    token = signature(path, expiry, secret=secret)
    query = urllib.parse.urlencode({"e": str(expiry), "t": token})
    return path + "?" + query


# Readable alias for callers that prefer a verb; both build the same path.
make_signed_path = signed_path


def verify(path, expires, token, secret=None, now=None):
    """Constant-time validation of ``token`` for exactly ``path``.

    Returns ``False`` for a non-signable path, a malformed expiry/token, an
    expired or far-future expiry, a wrong key or any tampering.  Never raises.
    """
    if not is_signable_path(path):
        return False
    if not isinstance(expires, str) or len(expires) > 12:
        return False
    if not _EXPIRY_RE.fullmatch(expires):
        return False
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LEN:
        return False
    if not _TOKEN_RE.fullmatch(token):
        return False
    try:
        expiry = int(expires)
        current = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        return False
    if expiry <= current or expiry > current + MAX_TOKEN_TTL:
        return False
    try:
        expected = signature(path, expires, secret=secret)
    except WebplayError:
        return False
    return hmac.compare_digest(token, expected)


def has_valid_signature(path, args, secret=None, now=None):
    """True when a request query carries a valid signature for ``path``.

    ``args`` is a Flask/Werkzeug ``MultiDict`` (or any mapping with ``get``).
    Malformed input and unexpected exceptions fail closed.
    """
    if not is_signable_path(path):
        return False
    if args is None:
        return False
    try:
        expires = args.get("e")
        token = args.get("t")
        if not isinstance(expires, str) or not isinstance(token, str):
            return False
        return verify(path, expires, token, secret=secret, now=now)
    except Exception:  # noqa: BLE001 - query data must never 500 the guard
        return False


__all__ = [
    "WebplayError",
    "SIGNED_PATH_RE",
    "TOKEN_TTL",
    "MAX_TOKEN_TTL",
    "is_signable_path",
    "signature",
    "signed_path",
    "make_signed_path",
    "verify",
    "has_valid_signature",
]
