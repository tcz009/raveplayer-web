# -*- coding: utf-8 -*-
"""Small in-process cache with negative entries and per-host single-flight.

The resolver runs in a Flask/gunicorn worker.  Two things matter:

* Do not hammer a provider for the same URL (positive cache) and do not
  repeatedly retry a URL that just 404'd (negative cache).
* Never let two threads resolve against the same host at the same time.  A
  per-host lock doubles as single-flight: the second caller waits, then hits
  the entry the first caller just stored, so the provider is called once.

The clock is injectable so TTL tests do not sleep.
"""

import collections
import threading
import time

from . import base

_MISS = object()


class CacheEntry(object):
    """One cached resolution or one cached failure."""

    __slots__ = ("value", "error", "expires_at")

    def __init__(self, value=None, error=None, expires_at=0.0):
        self.value = value
        self.error = error
        self.expires_at = expires_at

    @property
    def expired(self):
        return self.expires_at <= 0

    def __repr__(self):
        kind = "error" if self.error is not None else "value"
        return "CacheEntry(%s, expires_at=%r)" % (kind, self.expires_at)


def cacheable_error(error):
    """Only definitive failures belong in the negative cache.

    A timeout or rate-limit is transient: caching it would turn a short blip
    into a minute of failures.  Blocked/unknown/unsupported results are cheap
    to recompute and are not cached either.
    """
    if isinstance(error, base.UpstreamTimeout):
        return False
    if isinstance(error, base.RateLimited):
        return False
    if isinstance(error, base.NotFound):
        return True
    if isinstance(error, base.UpstreamError):
        return True
    return False


class _HostSlot(object):
    """One per-host lock plus an in-use count for safe LRU eviction.

    Evicting a lock while another thread is waiting on it would let a third
    caller create a second lock for the same host and break single-flight.
    Only idle slots are reclaimed.
    """

    __slots__ = ("lock", "users")

    def __init__(self):
        self.lock = threading.Lock()
        self.users = 0


class ResolverCache(object):
    """TTL + negative cache + per-host single-flight."""

    def __init__(self, positive_ttl=300.0, negative_ttl=15.0,
                 max_entries=2048, clock=time.time, max_host_locks=1024):
        self.positive_ttl = max(1.0, float(positive_ttl))
        self.negative_ttl = max(0.0, float(negative_ttl))
        self.max_entries = max(1, int(max_entries))
        self.max_host_locks = max(1, int(max_host_locks))
        self.clock = clock
        self._entries = collections.OrderedDict()
        self._lock = threading.RLock()
        self._host_locks = collections.OrderedDict()

    # -- public API -----------------------------------------------------

    def lookup(self, key):
        """Return a :class:`CacheEntry` or ``None``.  Never raises."""
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry

    def get(self, key):
        """Return the cached value, raise a cached error, or return ``None``."""
        entry = self.lookup(key)
        if entry is None:
            return None
        if entry.error is not None:
            raise entry.error
        return entry.value

    def store(self, key, resolved, ttl=None):
        """Store a successful resolution; respects a shorter URL expiry."""
        if not isinstance(resolved, base.Resolved):
            raise TypeError("resolved must be a Resolved")
        now = self.clock()
        lifetime = self.positive_ttl if ttl is None else max(0.0, float(ttl))
        expires = now + lifetime
        # A signed URL that expires before the cache TTL must not be served
        # after its real expiry.
        if resolved.expires_at and resolved.expires_at < expires:
            expires = float(resolved.expires_at)
        if expires <= now:
            return False
        with self._lock:
            self._entries[key] = CacheEntry(value=resolved, expires_at=expires)
            self._entries.move_to_end(key)
            self._trim_locked()
        return True

    def store_error(self, key, error, ttl=None):
        """Store a negative entry when the error is definitive."""
        if not isinstance(error, base.ResolverError):
            raise TypeError("error must be a ResolverError")
        if not cacheable_error(error):
            return False
        now = self.clock()
        lifetime = self.negative_ttl if ttl is None else max(0.0, float(ttl))
        if lifetime <= 0:
            return False
        with self._lock:
            self._entries[key] = CacheEntry(error=error, expires_at=now + lifetime)
            self._entries.move_to_end(key)
            self._trim_locked()
        return True

    def invalidate(self, key):
        with self._lock:
            self._entries.pop(key, None)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._host_locks.clear()

    def __len__(self):
        with self._lock:
            self._trim_locked()
            return len(self._entries)

    # -- single-flight --------------------------------------------------

    def run(self, key, host, factory):
        """Resolve *key* once per host, using the cache around *factory*.

        Concurrent calls for the same URL share the first result.  Calls for
        different URLs on the same host are serialised (the provider sees one
        request at a time) but both run.  Errors are cached only when
        :func:`cacheable_error` says they are definitive.
        """
        entry = self.lookup(key)
        if entry is not None:
            if entry.error is not None:
                raise entry.error
            if entry.value is not None:
                return entry.value

        host = str(host or "").lower()
        slot = self._acquire_host_slot(host)
        try:
            with slot.lock:
                # Another thread may have filled this key while we waited for
                # the host slot.
                entry = self.lookup(key)
                if entry is not None:
                    if entry.error is not None:
                        raise entry.error
                    if entry.value is not None:
                        return entry.value
                try:
                    resolved = factory()
                except base.ResolverError as exc:
                    self.store_error(key, exc)
                    raise
                if not isinstance(resolved, base.Resolved):
                    error = base.UpstreamError("resolver returned an invalid result")
                    self.store_error(key, error)
                    raise error
                self.store(key, resolved)
                return resolved
        finally:
            self._release_host_slot(host, slot)

    def _acquire_host_slot(self, host):
        with self._lock:
            slot = self._host_locks.get(host)
            if slot is None:
                slot = _HostSlot()
                self._host_locks[host] = slot
            slot.users += 1
            self._host_locks.move_to_end(host)
            return slot

    def _release_host_slot(self, host, slot):
        with self._lock:
            slot.users = max(0, slot.users - 1)
            if len(self._host_locks) <= self.max_host_locks:
                return
            # Evict only idle slots; a host with active users keeps its lock.
            for candidate in list(self._host_locks):
                if len(self._host_locks) <= self.max_host_locks:
                    break
                idle = self._host_locks.get(candidate)
                if idle is not None and idle.users == 0:
                    self._host_locks.pop(candidate, None)

    def _trim_locked(self):
        now = self.clock()
        # Drop expired entries first (bounded work), then enforce the cap.
        stale = [key for key, entry in self._entries.items()
                 if entry.expires_at <= now]
        for key in stale:
            self._entries.pop(key, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
