# -*- coding: utf-8 -*-
"""Shared fixtures for the Rave Player web tests.

Everything runs offline: providers are monkeypatched per test and the stream
proxy's DNS + connection layers are replaced with fakes.  Favourites and the
on-disk cache get a fresh directory per test.
"""

import os
import socket
import sys
import tempfile
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# These paths are read at import time by core.kodi / core.cache.
_BOOT = tempfile.mkdtemp(prefix="raveweb-test-boot-")
os.environ.setdefault("RAVEWEB_DATA", _BOOT)
os.environ.setdefault("RAVEPLAYER_CACHE_DIR", os.path.join(_BOOT, "cache"))
os.environ.setdefault("RAVEPLAYER_FAV_DIR", os.path.join(_BOOT, "favs"))
# Authentication is mandatory in 1.6.20; set a throwaway operator credential
# before app import, then log the shared `web` client in below.
os.environ.pop("RAVEWEB_PASSWORD_HASH", None)  # deterministic test credential
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["RAVEWEB_PASSWORD"] = "test-password"
os.environ["RAVEWEB_FRIENDS_PASSWORD"] = "friends-password"
os.environ["RAVEWEB_ENV"] = "test"

import app as app_module  # noqa: E402
from core import audit  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_audit_store(tmp_path):
    """Every test gets a fresh SQLite audit/session file."""
    audit.configure(str(tmp_path / "auth.sqlite3"))
    yield
    audit.configure(None)


@pytest.fixture(autouse=True)
def clear_process_caches():
    """Keep the in-process cache layer from leaking between tests."""
    from core import cache, moxser

    cache.clear_memory()
    moxser.clear_memory()
    yield
    cache.clear_memory()
    moxser.clear_memory()


@pytest.fixture(autouse=True)
def clear_login_limiter():
    app_module.LOGIN_LIMITER.clear()
    yield
    app_module.LOGIN_LIMITER.clear()


@pytest.fixture(autouse=True)
def isolated_slots():
    """Give every test a full stream-slot semaphore."""
    original = app_module._STREAM_SLOTS
    app_module._STREAM_SLOTS = threading.BoundedSemaphore(app_module.MAX_STREAMS)
    yield
    app_module._STREAM_SLOTS = original


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """Flask test client with isolated writable data directories."""
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(tmp_path / "favs"))
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(app_module, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    app_module._DATA_PROBE.update({"at": 0.0, "ok": True})
    app_module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    with app_module.app.test_client() as client:
        sid = audit.create_session("owner", "admin", "127.0.0.1", "pytest", 3600)
        with client.session_transaction() as sess:
            sess["auth"] = True
            sess["identity"] = "owner"
            sess["user"] = "owner"
            sess["role"] = "admin"
            sess["sid"] = sid
            sess["login_at"] = time.time()
            sess["remember"] = False
            sess["csrf"] = "test-csrf"
        yield client


@pytest.fixture()
def anon(tmp_path, monkeypatch):
    """An unauthenticated client (login/protected-route tests)."""
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(tmp_path / "favs"))
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(app_module, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    app_module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    app_module.LOGIN_LIMITER.clear()
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture()
def archive_search(monkeypatch):
    """Patch archive.search and expose the calls it received."""
    calls = []
    state = {
        "docs": [{"identifier": "pack-1", "title": "Pack One", "year": "1993"}],
        "total": 80,
    }

    def fake_search(query, page=1, rows=50, sort="downloads desc"):
        calls.append({"query": query, "page": page, "rows": rows, "sort": sort})
        return list(state["docs"]), state["total"]

    monkeypatch.setattr(app_module.archive, "search", fake_search)
    return {"calls": calls, "state": state}


@pytest.fixture()
def dns(monkeypatch):
    """Replace DNS lookups; the default answer is a public IPv4 address."""
    answers = {"addresses": ["93.184.216.34"], "error": None, "calls": []}

    def fake_getaddrinfo(host, port, **kwargs):
        answers["calls"].append((host, port))
        if answers["error"]:
            raise answers["error"]
        result = []
        for address in answers["addresses"]:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            result.append((family, socket.SOCK_STREAM, 6, "", (address, port or 80)))
        return result

    monkeypatch.setattr(app_module.socket, "getaddrinfo", fake_getaddrinfo)
    return answers
