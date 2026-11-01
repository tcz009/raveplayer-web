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

import collections
import copy
import hashlib
import json
import os
import tempfile
import threading
import time

from . import kodi

VERSION = 1
CACHE_DIR_ENV = "RAVEPLAYER_CACHE_DIR"
# A cache file is JSON we wrote ourselves; anything larger is either corrupt or
# was placed there deliberately, and must not be loaded into memory.
MAX_ENTRY_BYTES = 8 * 1024 * 1024
# Small in-process layer in front of the disk cache: warm API calls (feeds,
# archive metadata/search, Moxser pages) otherwise pay a file read + JSON parse
# on every request.  Values are deep-copied on the way in/out so callers cannot
# corrupt the shared entry.  The disk cache remains the source of truth across
# processes/workers; this is a bounded, best-effort speedup only.
MAX_MEMORY_ENTRIES = 256
_MEMORY = collections.OrderedDict()
_MEMORY_LOCK = threading.RLock()


def clear_memory():
    """Drop the in-process layer (tests/benchmarks; disk cache is untouched)."""
    with _MEMORY_LOCK:
        _MEMORY.clear()


def _memory_get(key):
    with _MEMORY_LOCK:
        record = _MEMORY.get(key)
        if record is None:
            return None
        _MEMORY.move_to_end(key)
        return record


def _memory_put(key, timestamp, value):
    with _MEMORY_LOCK:
        _MEMORY[key] = (timestamp, copy.deepcopy(value))
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > MAX_MEMORY_ENTRIES:
            _MEMORY.popitem(last=False)


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
        if os.path.getsize(path) > MAX_ENTRY_BYTES:
            _log("ignoring oversized entry %s" % key)
            _remove(path)
            return MISSING
    except FileNotFoundError:
        return MISSING
    except OSError:
        return MISSING
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
    # A payload without a usable value used to hand the MISSING sentinel back
    # as the value and re-read the same file forever: treat it as a miss.
    if "value" not in payload or payload["value"] is None:
        _remove(path)
        return MISSING
    return timestamp, payload["value"]


def _fresh(timestamp, ttl, now):
    age = now - timestamp
    if ttl is None:
        return True
    return ttl > 0 and 0 <= age <= ttl


def get(key, ttl, now=None):
    """Return a cached value, or :data:`MISSING` when absent/stale.

    ``ttl`` may be ``None`` for an entry that never expires.  Any disk or
    decode problem is logged at debug level and reported as a miss.
    """
    now = time.time() if now is None else now
    record = _memory_get(key)
    if record is not None:
        timestamp, value = record
        if _fresh(timestamp, ttl, now):
            return copy.deepcopy(value)
        with _MEMORY_LOCK:
            _MEMORY.pop(key, None)
    result = _read(key)
    if result is MISSING:
        return MISSING
    timestamp, value = result
    if not _fresh(timestamp, ttl, now):
        return MISSING
    _memory_put(key, timestamp, value)
    return copy.deepcopy(value)


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
        _memory_put(key, time.time(), value)
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
