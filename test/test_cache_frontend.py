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


# --------------------------------------------------------------------------- iOS / AirPlay


def test_audio_element_is_ios_and_airplay_ready():
    html = _read("templates/index.html")
    match = re.search(r'<audio\b[^>]*\bid="audio"[^>]*>', html)
    assert match, "audio element missing from index.html"
    tag = match.group(0)
    for attr in ('preload="none"', 'playsinline', 'webkit-playsinline',
                 'x-webkit-airplay="allow"'):
        assert attr in tag, "audio element is missing %s" % attr
    assert "controls" not in tag, "the custom controls must not be duplicated by native ones"
    # Exactly one slider: the seek bar. A volume range would be dead on iOS
    # because WebKit ignores HTMLMediaElement.volume there.
    assert html.count('type="range"') == 1


def test_airplay_button_starts_hidden_and_carries_state():
    html = _read("templates/index.html")
    match = re.search(r'<button\b[^>]*\bid="p-airplay"[^>]*>', html)
    assert match, "AirPlay button missing from index.html"
    tag = match.group(0)
    assert 'type="button"' in tag
    assert 'aria-pressed="false"' in tag
    assert "hidden" in tag, "the button must stay hidden until a track is loaded"


def test_airplay_code_reads_webkit_state_instead_of_guessing():
    js = _read("static/app.js")
    for marker in ("webkitShowPlaybackTargetPicker",
                   "webkitplaybacktargetavailabilitychanged",
                   "webkitcurrentplaybacktargetiswirelesschanged",
                   "webkitCurrentPlaybackTargetIsWireless",
                   "watchAvailability",
                   "remote.prompt",
                   "airplayIsWireless",
                   "aria-disabled",
                   "airplayReachabilityHint"):
        assert marker in js, "missing AirPlay marker %s" % marker


def test_airplay_degrades_for_non_lan_origins():
    js = _read("static/app.js")
    # A TV fetching the stream cannot reach localhost or, usually, a
    # Tailscale-only 100.x address; the UI must say so.
    assert "100\\." in js and "ts\\.net" in js
    assert "localhost" in js
    assert "same Wi-Fi" in js


def test_airplay_picker_call_stays_inside_the_gesture():
    js = _read("static/app.js")
    start = js.index("function onAirPlayClick")
    end = js.index("function setupRemotePlayback", start)
    block = js[start:end]
    assert "webkitShowPlaybackTargetPicker()" in block
    assert "await" not in block, "a gesture-gated picker call must not cross an await"


def test_live_now_resolution_accepts_station_lists():
    js = _read("static/app.js")
    assert "if (!url && Array.isArray(data.items))" in js
    assert 'typeof entry.play === "string"' in js


def test_ios_media_lifecycle_is_handled_in_source():
    js = _read("static/app.js")
    for marker in ("history.scrollRestoration", "visibilitychange", "pageshow",
                   "pagehide", "NotAllowedError", "IS_IOS",
                   "dataset.nowplaying", "queueSliderCommit",
                   "setPositionState(null)", "setAction(name, null)"):
        assert marker in js, "missing iOS/media marker %s" % marker


def test_no_dead_volume_control_or_volume_assignment():
    html = _read("templates/index.html")
    js = _read("static/app.js")
    assert "audio.volume" not in js, "iOS ignores audio.volume; hide any volume UI there"
    assert "volume" not in html.lower()


def test_seek_slider_commits_on_release_not_on_every_input():
    js = _read("static/app.js")
    start = js.index('rangeEl.addEventListener("input"')
    block = js[start:js.index("});", start)]
    assert "audio.currentTime" not in block, "dragging must not queue a seek per pixel"
    assert "seekFromSlider" in js
    assert "queueSliderCommit" in js


def test_mobile_shell_uses_safe_areas_and_dynamic_viewport():
    css = _read("static/app.css")
    for marker in ("100dvh", "-webkit-fill-available", "safe-area-inset-top",
                   "safe-area-inset-left", "safe-area-inset-right",
                   "overscroll-behavior", "touch-action: manipulation",
                   "min-height: 0", "max-height: calc(100dvh"):
        assert marker in css, "missing mobile layout marker %s" % marker


def test_help_dialog_has_touch_guidance_and_scrolls():
    html = _read("templates/index.html")
    css = _read("static/app.css")
    assert 'class="help-touch"' in html
    assert ".help-touch" in css
    assert "-webkit-overflow-scrolling: touch" in css
