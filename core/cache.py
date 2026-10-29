# -*- coding: utf-8 -*-
"""Small best-effort on-disk cache for network responses.

Kodi add-ons are killed and restarted constantly, so an in-memory cache would
not help much: the menu count for five podcast feeds would still mean five
HTTP requests on every load.  This stores one JSON file per key under the
add-on's profile directory and treats every disk operation as optional — a
read-only profile, a full disk or a corrupt file must never stop playback.

``ttl`` is in seconds.  Entries older than the TTL are ignored.  Only JSON
values are stored; callers keep JSON text or parsed JSON in the cache.
"""

import hashlib
import json
import os
import tempfile
import time

from . import kodi

VERSION = 1
CACHE_DIR_ENV = "RAVEPLAYER_CACHE_DIR"


class _Missing(object):
    """Sentinel distinct from a cached ``None``/empty value."""

    def __repr__(self):
        return "cache.MISSING"


MISSING = _Missing()


def _translate(path):
    """Turn a Kodi ``special://`` path into a real one when possible."""
    if not path.startswith("special://"):
        return path
    try:
        import xbmcvfs

        return xbmcvfs.translatePath(path)
    except Exception:
        return path


def _directory():
    """Return the cache directory, preferring the add-on profile."""
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return override
    try:
        profile = kodi.ADDON.getAddonInfo("profile") or ""
    except Exception:  # Kodi API is unavailable outside Kodi
        profile = ""
    if profile:
        return os.path.join(_translate(profile), "cache")
    # The smoke runner and unit tests have no real Kodi profile.
    return os.path.join(tempfile.gettempdir(), "raveplayer-cache")


def _log(message, level=10):
    try:
        kodi.log("cache: " + message, level=level)
    except Exception:
        pass


def path_for(key):
    """Return the file path for a cache key (exposed for tests/debugging)."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(_directory(), digest + ".json")


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _read(key):
    path = path_for(key)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return MISSING
    except (OSError, ValueError, TypeError) as exc:
        _log("ignoring unreadable entry %s (%s)" % (key, exc))
        _remove(path)
        return MISSING

    if not isinstance(payload, dict) or payload.get("v") != VERSION:
        _remove(path)
        return MISSING
    timestamp = payload.get("ts")
    if not isinstance(timestamp, (int, float)):
        _remove(path)
        return MISSING
    value = payload.get("value", MISSING)
    if value is None:
        return MISSING
    return timestamp, value


def get(key, ttl, now=None):
    """Return a cached value, or :data:`MISSING` when absent/stale.

    ``ttl`` may be ``None`` for an entry that never expires.  Any disk or
    decode problem is logged at debug level and reported as a miss.
    """
    result = _read(key)
    if result is MISSING:
        return MISSING
    timestamp, value = result
    now = time.time() if now is None else now
    age = now - timestamp
    if ttl is not None and (ttl <= 0 or age < 0 or age > ttl):
        return MISSING
    return value


def put(key, value):
    """Store a JSON-serialisable value.  Returns True on success."""
    if value is None:
        return False
    path = path_for(key)
    temporary = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(path),
            prefix=".tmp-", suffix=".json", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(
                {"v": VERSION, "ts": time.time(), "value": value},
                handle, ensure_ascii=False, separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        _log("could not write %s (%s)" % (key, exc))
        if temporary:
            _remove(temporary)
        return False


def remember(key, ttl, producer):
    """Return a cached value, otherwise call ``producer`` and cache its result.

    ``producer`` results of ``None`` are treated as failures and are not
    cached, so a temporary network error does not stick for the whole TTL.
    """
    value = get(key, ttl)
    if value is not MISSING:
        return value
    value = producer()
    if value is not None:
        put(key, value)
    return value
