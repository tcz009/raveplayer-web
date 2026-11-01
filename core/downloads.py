# -*- coding: utf-8 -*-
"""Server-side Downloads store (web 1.6.16; per-user scopes in 1.6.26).

Only explicit, direct-file saves are kept here: the app writes one file plus a
small JSON index under ``<RAVEWEB_DATA>/downloads``.  This module owns the
filesystem side (safe filenames, atomic index writes, size/space guards); the
network side (SSRF-validated fetch) lives in ``app.py`` so it can reuse the
stream proxy's resolver, redirect checks and timeouts.

``identity=None``/``"owner"`` uses the legacy root ``downloads/`` directory
(that is how the pre-1.6.26 global library is adopted as the owner's library,
without moving files).  Other identities get an isolated
``downloads/<identity>/`` directory.  Existing tests and callers that omit
identity keep the legacy owner behaviour.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata

try:  # POSIX only, like core/favourites.py
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

DIRNAME = "downloads"
INDEXNAME = "index.json"
LOCKNAME = ".downloads.lock"
TEMP_PREFIX = ".tmp-"
ALLOWED_EXTENSIONS = (".mp3", ".m4a")
MAX_FILENAME = 110

_LOCK = threading.RLock()
_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_IDENT_RE = re.compile(r"[^A-Za-z0-9_-]+")


class StorageError(Exception):
    """The download cannot be stored (count, size or free-space guard)."""


def _identity_name(identity):
    if identity in (None, "", "owner"):
        return ""
    safe = _IDENT_RE.sub("", str(identity))[:32]
    return safe or "friends"


def _scope_dir(base_dir, identity=None):
    path = os.path.join(base_dir, DIRNAME)
    name = _identity_name(identity)
    return os.path.join(path, name) if name else path


def directory(base_dir, identity=None):
    path = _scope_dir(base_dir, identity)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def index_path(base_dir, identity=None):
    return os.path.join(directory(base_dir, identity), INDEXNAME)


class _StoreLock(object):
    """Process lock + best-effort advisory file lock (same idea as favourites)."""

    def __init__(self, base_dir, identity=None):
        self._base = base_dir
        self._identity = identity
        self._handle = None

    def __enter__(self):
        _LOCK.acquire()
        try:
            self._handle = open(os.path.join(directory(self._base, self._identity), LOCKNAME), "a+")
        except OSError:
            self._handle = None
        if self._handle is not None and fcntl is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        _LOCK.release()
        return False


def extension_for(url):
    """Return the allowed extension for a URL path, or ""."""
    if not isinstance(url, str):
        return ""
    try:
        path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    except Exception:  # noqa: BLE001 - defensive on odd strings
        return ""
    for extension in ALLOWED_EXTENSIONS:
        if path.endswith(extension):
            return extension
    return ""


def safe_stem(title, limit=MAX_FILENAME):
    """A filesystem-safe, human-readable stem (no path separators/dots-only)."""
    text = unicodedata.normalize("NFKD", str(title or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _SAFE_RE.sub(" ", text)
    text = re.sub(r"\s+", "-", text).strip("-._ ")
    text = re.sub(r"-{2,}", "-", text)[:limit].strip("-")
    if not text or text in (".", ".."):
        text = "download"
    return text


def _valid_filename(name):
    return (
        isinstance(name, str)
        and name
        and os.path.basename(name) == name
        and ".." not in name
        and name[0] not in "._"
        and len(name) <= 200
    )


def _read_unlocked(base_dir, identity=None):
    path = index_path(base_dir, identity)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError):
        return []
    entries = []
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict):
            continue
        identifier = raw.get("id")
        filename = raw.get("filename")
        if not isinstance(identifier, str) or not identifier:
            continue
        if not _valid_filename(filename):
            continue
        try:
            size = int(raw.get("size") or 0)
            created = float(raw.get("created") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        entries.append({
            "id": identifier,
            "title": str(raw.get("title") or "Untitled")[:300],
            "filename": filename,
            "source": str(raw.get("source") or ""),
            "url": str(raw.get("url") or ""),
            "size": max(0, size),
            "created": created,
        })
    return entries


def _write_unlocked(base_dir, entries, identity=None):
    path = index_path(base_dir, identity)
    directory_path = os.path.dirname(path)
    try:
        os.makedirs(directory_path, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory_path,
            prefix=".tmp-index-", suffix=".json", delete=False)
        try:
            json.dump(entries, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        finally:
            handle.close()
        os.replace(temp_name, path)
        return True
    except OSError:
        return False


def used(base_dir, identity=None):
    return sum(entry["size"] for entry in _read_unlocked(base_dir, identity))


def find(base_dir, download_id, identity=None):
    if not isinstance(download_id, str):
        return None
    return next((entry for entry in _read_unlocked(base_dir, identity)
                 if entry["id"] == download_id), None)


def find_url(base_dir, url, identity=None):
    if not isinstance(url, str):
        return None
    return next((entry for entry in _read_unlocked(base_dir, identity)
                 if entry["url"] == url), None)


def file_path(base_dir, entry, identity=None):
    return os.path.join(directory(base_dir, identity), entry["filename"])


def temp_path(base_dir, entry, identity=None):
    return os.path.join(directory(base_dir, identity), TEMP_PREFIX + entry["filename"])


def entry_for(base_dir, title, url, source):
    """Build (but do not store) the metadata for one download."""
    extension = extension_for(url)
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]
    filename = "%s-%s%s" % (safe_stem(title), digest[:8], extension or ".mp3")
    return {
        "id": digest,
        "title": str(title or "Untitled").strip()[:300] or "Untitled",
        "filename": filename,
        "source": str(source or ""),
        "url": str(url),
        "size": 0,
        "created": time.time(),
    }


def _free_bytes(base_dir, identity=None):
    try:
        return shutil.disk_usage(directory(base_dir, identity)).free
    except OSError:
        return 0


def reserve(base_dir, max_items, identity=None):
    """Check the count guard before spending time on a download."""
    with _StoreLock(base_dir, identity):
        entries = _read_unlocked(base_dir, identity)
        if len(entries) >= max_items:
            raise StorageError("the Downloads library is full (%d items)" % max_items)


def register(base_dir, entry, temp_name, max_total, max_items, identity=None):
    """Atomically move the finished temp file into place and index it."""
    size = entry.get("size") or 0
    with _StoreLock(base_dir, identity):
        entries = [item for item in _read_unlocked(base_dir, identity)
                   if item["id"] != entry["id"]]
        if len(entries) >= max_items:
            raise StorageError("the Downloads library is full (%d items)" % max_items)
        total = sum(item["size"] for item in entries)
        if total + size > max_total:
            raise StorageError("the Downloads library is full (size cap reached)")
        if _free_bytes(base_dir, identity) < size + 1024 * 1024:
            raise StorageError("not enough disk space for this download")
        destination = os.path.join(directory(base_dir, identity), entry["filename"])
        try:
            os.replace(temp_name, destination)
        except OSError as exc:
            raise StorageError("could not store the download (%s)" % exc)
        entry["created"] = time.time()
        entries.append(entry)
        entries.sort(key=lambda item: item.get("created") or 0, reverse=True)
        if not _write_unlocked(base_dir, entries, identity):
            try:
                os.remove(destination)
            except OSError:
                pass
            raise StorageError("could not update the Downloads index")
        return entry


def discard(base_dir, entry, identity=None):
    name = temp_path(base_dir, entry, identity)
    try:
        os.remove(name)
    except OSError:
        pass


def remove(base_dir, download_id, identity=None):
    """Delete one stored download; returns True when something was removed."""
    with _StoreLock(base_dir, identity):
        entries = _read_unlocked(base_dir, identity)
        match = next((item for item in entries if item["id"] == download_id), None)
        if match is None:
            return False
        try:
            os.remove(os.path.join(directory(base_dir, identity), match["filename"]))
        except OSError:
            pass
        entries = [item for item in entries if item["id"] != download_id]
        _write_unlocked(base_dir, entries, identity)
        return True


def list_entries(base_dir, identity=None):
    """Index entries whose file still exists, newest first."""
    folder = directory(base_dir, identity)
    entries = [entry for entry in _read_unlocked(base_dir, identity)
               if os.path.isfile(os.path.join(folder, entry["filename"]))]
    entries.sort(key=lambda item: item.get("created") or 0, reverse=True)
    return entries


def all_entries(base_dir):
    """Owner view: owner legacy scope plus every isolated identity scope."""
    entries = []
    for entry in list_entries(base_dir, None):
        entry = dict(entry, identity="owner")
        entries.append(entry)
    root = _scope_dir(base_dir)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if not name or name.startswith(".") or name == DIRNAME:
            continue
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        for entry in list_entries(base_dir, name):
            entries.append(dict(entry, identity=name))
    entries.sort(key=lambda item: item.get("created") or 0, reverse=True)
    return entries


def migrate_legacy(base_dir):
    """Adopt the pre-1.6.26 global library as the owner's library.

    The legacy files already live in the owner scope (the root ``downloads/``
    directory), so this is deliberately a no-op for files: it validates the
    owner index/files and returns counts.  It is idempotent; friends scopes are
    untouched/empty.
    """
    return {
        "owner": len(list_entries(base_dir, None)),
        "friends": len(list_entries(base_dir, "friends")),
    }
