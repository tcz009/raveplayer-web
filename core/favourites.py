# -*- coding: utf-8 -*-
"""Favourites: a small JSON list in the add-on profile directory.

Best-effort like the cache — a read-only profile must never break the add-on,
it just means favourites are not saved.
"""

import json
import os
import tempfile
import threading

try:  # POSIX only; the container is Linux, macOS dev boxes too
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from . import kodi

FILENAME = "favourites.json"
LOCKNAME = ".favourites.lock"
MAX_ENTRIES = 300
FAV_DIR_ENV = "RAVEPLAYER_FAV_DIR"

# A process-wide lock serialises read-modify-write cycles inside one worker;
# the advisory file lock does the same across gunicorn workers.  Without this,
# concurrent POST/DELETE requests can each read the same list and the last save
# silently discards the other change.
_LOCK = threading.RLock()


def _translate(path):
    if not path.startswith("special://"):
        return path
    try:
        import xbmcvfs

        return xbmcvfs.translatePath(path)
    except Exception:
        return path


def _path():
    override = os.environ.get(FAV_DIR_ENV)
    if override:
        directory = override
    else:
        try:
            profile = kodi.ADDON.getAddonInfo("profile") or ""
        except Exception:
            profile = ""
        directory = _translate(profile) if profile else os.path.join(tempfile.gettempdir(), "raveplayer-favs")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    return os.path.join(directory, FILENAME)


class _StoreLock(object):
    """Re-entrant thread lock plus a best-effort exclusive file lock.

    Locking must never turn a read-only data dir into an error: if the lock
    file cannot be created or locked, the caller still runs (same best-effort
    policy as the rest of this module) under the process lock only.
    """

    def __init__(self):
        self._handle = None

    def __enter__(self):
        _LOCK.acquire()
        directory = os.path.dirname(_path())
        try:
            os.makedirs(directory, exist_ok=True)
            self._handle = open(os.path.join(directory, LOCKNAME), "a+")
        except OSError:
            self._handle = None
        if self._handle is not None and fcntl is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
        return self

    def __exit__(self, exc_type, exc, traceback):
        handle, self._handle = self._handle, None
        if handle is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass
        _LOCK.release()
        return False


def load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict) and entry.get("title")]


def save(entries):
    """Write the list atomically: same-dir temp file, fsync, then replace.

    A crash or full disk in the middle of a plain ``open(..., "w")`` could
    truncate the store and lose every favourite (audit risk 7).  The temp
    file lives next to the real one so ``os.replace`` stays on one
    filesystem, and the previous file is only swapped out once the new JSON
    is safely on disk.  The stored shape (a plain JSON list) is unchanged.
    """
    path = _path()
    directory = os.path.dirname(path)
    temporary = None
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".tmp-", suffix=".json", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(entries[:MAX_ENTRIES], handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError):
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass
        return False


def key(entry):
    return "%s:%s" % (entry.get("kind", ""), entry.get("target", ""))


def add(entry):
    """Add or refresh an entry; returns True when the list changed."""
    if not entry.get("title") or not entry.get("target"):
        return False
    with _StoreLock():
        entries = [item for item in load() if key(item) != key(entry)]
        entries.insert(0, {
            "kind": entry["kind"],
            "target": entry["target"],
            "title": entry["title"][:200],
            "source": entry.get("source", ""),
        })
        return save(entries)


def remove(entry_key):
    with _StoreLock():
        entries = [item for item in load() if key(item) != entry_key]
        return save(entries)


def has(entry):
    return any(key(item) == key(entry) for item in load())
