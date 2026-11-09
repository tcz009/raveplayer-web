# -*- coding: utf-8 -*-
"""One process-wide concurrency budget for every proxied media stream.

Both ``/api/stream`` (the generic proxy in ``app.py``) and
``/api/mega/stream`` (the decrypting MEGA proxy) hold a gunicorn ``gthread``
thread for the life of a playback.  Before this module each route had its own
semaphore, so a worker with 8 threads could admit 6 generic + 4 MEGA streams
and starve the JSON API and login.  A single shared semaphore fixes that
composition problem; the per-route caps remain as narrower limits.

The budget is *per process*.  A deployment with ``-w 2`` therefore has
``2 × RAVEWEB_STREAM_SLOTS`` total stream threads, never ``8 + 4`` per worker.

Configuration (read once at import, then resettable for tests):

* ``RAVEWEB_STREAM_SLOTS`` — explicit total slots (1..64).
* ``RAVEWEB_STREAM_WORKERS`` — gthread count, default 8.
* ``RAVEWEB_STREAM_RESERVE`` — threads to keep free for JSON/login, default 2.

With no explicit value the default is ``8 - 2 = 6``, which is deliberately
below a standard 8-thread gthread worker.  That is a safe default for the
shipped gunicorn command; operators who change ``--threads`` can set
``RAVEWEB_STREAM_SLOTS`` to the new safe value.
"""

from __future__ import annotations

import os
import threading

DEFAULT_SLOTS = 6
DEFAULT_WORKERS = 8
DEFAULT_RESERVE = 2
MAX_SLOTS = 64

_LOCK = threading.Lock()


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def configured_slots():
    """Return the total per-process stream budget from the environment."""
    explicit = (os.environ.get("RAVEWEB_STREAM_SLOTS") or "").strip()
    if explicit:
        return _env_int("RAVEWEB_STREAM_SLOTS", DEFAULT_SLOTS, 1, MAX_SLOTS)
    workers = _env_int("RAVEWEB_STREAM_WORKERS", DEFAULT_WORKERS, 1, 256)
    reserve = _env_int("RAVEWEB_STREAM_RESERVE", DEFAULT_RESERVE, 0, 64)
    return max(1, min(workers - reserve, MAX_SLOTS))


_SLOTS = threading.BoundedSemaphore(configured_slots())


def reset_slots(total=None):
    """Replace the shared semaphore (tests, config reload, worker fork).

    ``total`` may be an explicit slot count; otherwise the environment is
    re-read.  Callers must not be holding the old semaphore when this runs.
    """
    global _SLOTS
    with _LOCK:
        slots = configured_slots() if total is None else max(1, min(int(total), MAX_SLOTS))
        _SLOTS = threading.BoundedSemaphore(slots)


def acquire(timeout=2.0):
    """Take one shared stream slot; return ``False`` instead of blocking forever."""
    try:
        return bool(_SLOTS.acquire(timeout=timeout))
    except (ValueError, TypeError):
        return False


def release():
    """Return a slot; safe to call from cleanup paths (double release ignored)."""
    try:
        _SLOTS.release()
    except (ValueError, AttributeError, TypeError):
        pass


def available():
    """Best-effort free-slot count for diagnostics/tests (not a reservation)."""
    slots = _SLOTS
    try:
        return int(getattr(slots, "_value", 0))
    except Exception:  # pragma: no cover - BoundedSemaphore always has _value
        return 0


__all__ = ["acquire", "available", "configured_slots", "release", "reset_slots"]
