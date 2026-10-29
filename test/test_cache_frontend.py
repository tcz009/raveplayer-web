# -*- coding: utf-8 -*-
"""Cache hygiene plus static front-end sanity checks (no browser needed)."""

import os
import re
import shutil
import subprocess
import time

import pytest

from core import cache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- cache


def test_cache_put_get(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    assert cache.get("missing", 60) is cache.MISSING
    assert cache.put("key", {"a": [1, 2, 3]}) is True
    assert cache.get("key", 60) == {"a": [1, 2, 3]}


def test_cache_ttl_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.put("key", "value")
    assert cache.get("key", 60) == "value"
    assert cache.get("key", 1, now=time.time() + 10) is cache.MISSING


def test_cache_remember_does_not_store_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    calls = []

    def producer():
        calls.append(1)
        return None

    assert cache.remember("key", 60, producer) is None
    assert cache.remember("key", 60, producer) is None
    assert len(calls) == 2


def test_cache_prune_removes_only_stale_entries(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    directory = tmp_path / "cache"
    directory.mkdir()
    old = directory / ("a" * 64 + ".json")
    fresh = directory / ("b" * 64 + ".json")
    leftover = directory / ".tmp-abc.json"
    unrelated = directory / "notes.txt"
    for path in (old, fresh, leftover, unrelated):
        path.write_text("{}", encoding="utf-8")

    ancient = time.time() - 30 * 24 * 3600
    os.utime(old, (ancient, ancient))
    os.utime(leftover, (ancient, ancient))

    app_module._prune_cache()

    assert not old.exists()
    assert fresh.exists()
    assert not leftover.exists()
    assert unrelated.exists()


def test_cache_prune_survives_missing_dir(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "does-not-exist"))
    app_module._prune_cache()  # must not raise


# --------------------------------------------------------------------------- front-end


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_every_javascript_element_id_exists_in_the_template():
    js = _read("static/app.js")
    html = _read("templates/index.html")
    ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
    assert ids, "no element lookups found in app.js"
    missing = sorted(identifier for identifier in ids if 'id="%s"' % identifier not in html)
    assert not missing, "app.js looks up ids missing from index.html: %s" % missing


def test_no_innerhtml_in_the_ui():
    js = _read("static/app.js")
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, "--check", os.path.join(ROOT, "static/app.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_media_session_and_keyboard_hooks_are_present():
    js = _read("static/app.js")
    for marker in ("navigator.mediaSession", "MediaMetadata", "setActionHandler",
                   "seekbackward", "nexttrack", "ArrowLeft", "ArrowRight",
                   "popstate", "hashchange", "navigator.onLine"):
        assert marker in js, "missing %s" % marker


def test_template_has_accessibility_hooks():
    html = _read("templates/index.html")
    for marker in ('class="skip"', 'role="dialog"', 'aria-modal="true"',
                   'aria-live="polite"', 'aria-label="Search"',
                   'for="search-input"', 'aria-expanded="false"',
                   'aria-pressed="false"', 'name="viewport"'):
        assert marker in html, "missing %s" % marker


def test_css_has_focus_playing_and_mobile_rules():
    css = _read("static/app.css")
    for marker in (":focus-visible", ".row.playing", "prefers-reduced-motion",
                   "safe-area-inset-bottom", "--topbar-h", "pointer: coarse"):
        assert marker in css, "missing %s" % marker


def test_template_references_only_existing_static_files():
    html = _read("templates/index.html")
    for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        path = src.replace("/static/", "static/", 1)
        assert os.path.exists(os.path.join(ROOT, path)), "missing asset %s" % src
