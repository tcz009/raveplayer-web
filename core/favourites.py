# -*- coding: utf-8 -*-
"""Favourites: a small JSON list, scoped per signed-in identity (1.6.26).

The pre-1.6.26 global file remains the **owner's** store (so existing
favourites are adopted without a file move); other identities get
``<favourites-root>/<identity>/favourites.json``.  ``identity=None`` keeps the
legacy behaviour for module tests/tools.

Best-effort like the cache — a read-only profile must never break the app, it
just means favourites are not saved.
"""

import json
import os
import re
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

_LOCK = threading.RLock()
_IDENT_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _translate(path):
    if not path.startswith("special://"):
        return path
    try:
        import xbmcvfs

        return xbmcvfs.translatePath(path)
    except Exception:
        return path


def _root():
    override = os.environ.get(FAV_DIR_ENV)
    if override:
        directory = override
    else:
        try:
            profile = kodi.ADDON.getAddonInfo("profile") or ""
        except Exception:
            profile = ""
        directory = _translate(profile) if profile else os.path.join(tempfile.gettempdir(), "raveplayer-favs")
    return directory


def _identity_name(identity):
    if identity in (None, "", "owner"):
        return ""
    safe = _IDENT_RE.sub("", str(identity))[:32]
    return safe or "friends"


def _path(identity=None):
    root = _root()
    name = _identity_name(identity)
    directory = os.path.join(root, name) if name else root
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    return os.path.join(directory, FILENAME)


class _StoreLock(object):
    """Re-entrant thread lock plus a best-effort exclusive file lock."""

    def __init__(self, identity=None):
        self._identity = identity
        self._handle = None

    def __enter__(self):
        _LOCK.acquire()
        directory = os.path.dirname(_path(self._identity))
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


def _load_from(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict) and entry.get("title")]


def load(identity=None):
    return _load_from(_path(identity))


def save(entries, identity=None):
    """Write the list atomically in the identity's directory."""
    path = _path(identity)
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


def add(entry, identity=None):
    """Add or refresh an entry; returns True when the list changed."""
    if not entry.get("title") or not entry.get("target"):
        return False
    with _StoreLock(identity):
        entries = [item for item in load(identity) if key(item) != key(entry)]
        entries.insert(0, {
            "kind": entry["kind"],
            "target": entry["target"],
            "title": entry["title"][:200],
            "source": entry.get("source", ""),
        })
        return save(entries) if identity is None else save(entries, identity)


def remove(entry_key, identity=None):
    with _StoreLock(identity):
        entries = [item for item in load(identity) if key(item) != entry_key]
        return save(entries) if identity is None else save(entries, identity)


def has(entry, identity=None):
    return any(key(item) == key(entry) for item in load(identity))


def all_entries():
    """Owner view: owner legacy store plus isolated identity stores."""
    root = _root()
    entries = [dict(entry, identity="owner") for entry in _load_from(_path(None))]
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if not name or name.startswith(".") or name in ("owner",):
            continue
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        for entry in _load_from(os.path.join(folder, FILENAME)):
            entries.append(dict(entry, identity=name))
    return entries


def migrate_legacy():
    """Adopt the pre-1.6.26 global file as the owner's store (no file move)."""
    return {"owner": len(_load_from(_path(None))), "friends": len(_load_from(_path("friends")))}
