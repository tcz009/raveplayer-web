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


def get(key, ttl, now=None, copy_value=True):
    """Return a cached value, or :data:`MISSING` when absent/stale.

    ``ttl`` may be ``None`` for an entry that never expires.  Any disk or
    decode problem is logged at debug level and reported as a miss.

    ``copy_value=False`` returns the shared in-process object instead of a deep
    copy.  Only use it for callers that treat the value as read-only: a large
    parsed feed (DejaVu FM's 4,000-episode list) otherwise costs a ~50 ms deep
    copy on every /api/latest.  The disk path still returns a fresh object, so
    a cold process can never alias another caller's mutation.
    """
    now = time.time() if now is None else now
    record = _memory_get(key)
    if record is not None:
        timestamp, value = record
        if _fresh(timestamp, ttl, now):
            return copy.deepcopy(value) if copy_value else value
        with _MEMORY_LOCK:
            _MEMORY.pop(key, None)
    result = _read(key)
    if result is MISSING:
        return MISSING
    timestamp, value = result
    if not _fresh(timestamp, ttl, now):
        return MISSING
    _memory_put(key, timestamp, value)
    return copy.deepcopy(value) if copy_value else value


def age(key, now=None):
    """Seconds since the entry was written, or ``None`` when it is absent.

    Unlike :func:`get` this has no TTL argument and never applies an expiry
    decision: callers use it to implement their own freshness policy (for
    example stale-while-revalidate).  The value is never deep-copied, so an
    age check on a large parsed feed is a timestamp read rather than another
    copy of the feed.  A clock that moved backwards is clamped to zero.
    """
    now = time.time() if now is None else now
    with _MEMORY_LOCK:
        record = _MEMORY.get(key)
        if record is not None:
            return max(0.0, now - record[0])
    result = _read(key)
    if result is MISSING:
        return None
    timestamp, _value = result
    return max(0.0, now - timestamp)


def has(key, ttl, now=None):
    """True when :func:`get` would return a value, without copying it.

    ``fetch.is_cached`` uses this on multi-hundred-KB scraped pages: a warm
    Toronto/Moxser view must not deep-copy the body just to decide whether to
    sleep for the courtesy gap.
    """
    now = time.time() if now is None else now
    with _MEMORY_LOCK:
        record = _MEMORY.get(key)
        if record is not None:
            if _fresh(record[0], ttl, now):
                return True
            _MEMORY.pop(key, None)
    result = _read(key)
    if result is MISSING:
        return False
    timestamp, _value = result
    return _fresh(timestamp, ttl, now)


def forget(key):
    """Drop a cached entry (memory and disk), used to clear a failure mark."""
    with _MEMORY_LOCK:
        _MEMORY.pop(key, None)
    try:
        _remove(path_for(key))
    except OSError:
        pass


def put(key, value, sync=True):
    """Store a JSON-serialisable value.  Returns True on success.

    ``sync=False`` skips ``fsync`` for derived/recomputable entries (parsed
    pages).  The atomic rename still makes readers see either the old or the
    new file; a power loss may drop a derived entry, and the next read simply
    re-parses.  HTTP bodies keep the default durable write.
    """
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
            if sync:
                os.fsync(handle.fileno())
        os.replace(temporary, path)
        _memory_put(key, time.time(), value)
        return True
    except (OSError, TypeError, ValueError) as exc:
        _log("could not write %s (%s)" % (key, exc))
        if temporary:
            _remove(temporary)
        return False


def remember(key, ttl, producer, sync=True):
    """Return a cached value, otherwise call ``producer`` and cache its result.

    ``producer`` results of ``None`` are treated as failures and are not
    cached, so a temporary network error does not stick for the whole TTL.
    ``sync=False`` skips fsync for derived/recomputable entries (raw HTTP
    bodies): a lost write after a power cut just means one more fetch, and the
    fsync is a per-request disk stall on the NAS.
    """
    value = get(key, ttl)
    if value is not MISSING:
        return value
    value = producer()
    if value is not None:
        put(key, value, sync=sync)
    return value


def fingerprint(text):
    """Stable (cross-process) fingerprint of an exact page body."""
    return hashlib.blake2b(str(text).encode("utf-8", "replace"),
                           digest_size=8).hexdigest()


def parsed(key, ttl, body, parser):
    """Return the parsed form of ``body``, re-parsing only when it changes.

    The parsed object lives beside the raw body, so a warm request skips the
    regex/XML pass and only compares a fingerprint.  ``parser`` results must be
    JSON-safe lists or dicts; anything else is returned uncached.
    """
    if body is None:
        return None
    digest = fingerprint(body)
    record = get(key, ttl)
    if isinstance(record, dict) and record.get("fingerprint") == digest:
        value = record.get("value")
        if isinstance(value, (list, dict)):
            return value
    value = parser(body)
    if isinstance(value, (list, dict)):
        put(key, {"fingerprint": digest, "value": value}, sync=False)
    return value
