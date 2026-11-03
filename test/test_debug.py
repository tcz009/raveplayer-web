# -*- coding: utf-8 -*-
"""1.6.31–1.6.32 — opt-in client debug log, menu switch and request ids.

The JavaScript unit checks (toggle, redaction, ring buffer) run under Node so
they exercise the real ``static/debug.js`` rather than strings in it. The
server tests cover the ``X-Request-ID`` contract and the log correlation.
"""

import logging
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
NODE = shutil.which("node")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------- client


def test_index_loads_the_debug_log_before_the_app():
    html = _read("templates/index.html")
    debug_at = html.index("/static/debug.js")
    app_at = html.index("/static/app.js")
    assert debug_at < app_at, "debug.js must install before app.js boots"
    assert 'defer' in html[debug_at:debug_at + 80]
    # Off by default: no panel markup is shipped in the page.
    assert 'id="debug-panel"' not in html


def test_help_dialog_has_a_labelled_debug_toggle():
    """1.6.32: the switch lives in the existing Help/utility dialog."""
    html = _read("templates/index.html")
    match = re.search(r'<button\b[^>]*\bid="debug-toggle"[^>]*>(.*?)</button>',
                      html, re.S)
    assert match, "the Help dialog has no #debug-toggle button"
    tag, label = match.group(0), match.group(1)
    assert 'type="button"' in tag
    assert 'class="debug-toggle"' in tag
    assert 'aria-pressed="false"' in tag
    assert 'aria-label="Debug log: off' in tag
    assert "Debug log: off" in label


def test_debug_toggle_has_focus_and_44px_mobile_styling():
    css = _read("static/app.css")
    start = css.index(".debug-toggle {")
    block = css[start:css.index("}", start)]
    assert "min-height: 44px" in block
    assert ".debug-toggle:focus-visible" in css
    coarse = css[css.index("@media (pointer: coarse)"):]
    coarse = coarse[:coarse.index("}")]
    assert ".debug-toggle" in coarse


def test_debug_js_owns_the_menu_switch():
    js = _read("static/debug.js")
    assert "function setEnabled" in js
    assert "document.getElementById(\"debug-toggle\")" in js
    assert 'button.textContent = "Debug log: " + (on ? "on" : "off")' in js
    assert "setEnabled: setEnabled" in js


def test_debug_log_is_local_only():
    js = _read("static/debug.js")
    assert "sendBeacon" not in js
    assert "new XMLHttpRequest" not in js
    assert "XMLHttpRequest" not in js
    # The only network calls are the app's own: we wrap fetch, never call it.
    assert "window.fetch = wrapped" in js
    for token in ("Copy", "Download", "Clear", 'panel.id = "debug-panel"'):
        assert token in js, token
    assert "MAX_EVENTS = 500" in js


def test_debug_log_is_wired_into_the_app():
    js = _read("static/app.js")
    assert "window.RaveDebug" in js
    assert "function debugEvent" in js
    for kind in ('debugEvent("route"', 'debugEvent("download"'):
        assert kind in js, kind
    assert "X-Request-ID" in _read("static/debug.js")


def test_service_worker_reports_state_to_the_debug_log():
    js = _read("static/sw-register.js")
    assert "RaveDebug" in js and "rave:sw" in js
    assert "statechange" in js


@pytest.mark.skipif(NODE is None, reason="Node is required for the JS unit checks")
def test_debug_js_unit_checks():
    """Toggle, redaction and the 500-event ring buffer, run against the real JS."""
    script = os.path.join(HERE, "js", "debug_spec.js")
    result = subprocess.run([NODE, script], cwd=ROOT, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS 13 debug.js unit checks" in result.stdout


# --------------------------------------------------------------------------- server


def test_response_carries_a_generated_request_id(web):
    first = web.get("/api/menu")
    assert first.status_code == 200
    request_id = first.headers.get("X-Request-ID")
    assert REQUEST_ID_RE.match(request_id or ""), request_id
    second = web.get("/api/menu")
    assert second.headers.get("X-Request-ID") != request_id, "ids must be unique"


def test_client_request_id_is_echoed_for_correlation(web):
    supplied = "debug-test-8f3a1c"
    response = web.get("/api/menu", headers={"X-Request-ID": supplied})
    assert response.headers.get("X-Request-ID") == supplied


@pytest.mark.parametrize("bad", ["short", "spaces in here", "x" * 100, "semi;colon"])
def test_invalid_request_ids_are_replaced_not_reflected(web, bad):
    response = web.get("/api/menu", headers={"X-Request-ID": bad})
    returned = response.headers.get("X-Request-ID")
    assert returned != bad
    assert REQUEST_ID_RE.match(returned or ""), returned


def test_request_log_line_carries_the_id(web, caplog):
    supplied = "correlate-me-1234"
    with caplog.at_level(logging.INFO):
        response = web.get("/api/menu?token=should-not-be-logged",
                           headers={"X-Request-ID": supplied})
    assert response.headers.get("X-Request-ID") == supplied
    lines = [record.getMessage() for record in caplog.records
             if "req id=" in record.getMessage()]
    assert lines, "no request line was logged"
    matching = [line for line in lines if supplied in line]
    assert matching, lines
    assert "path=/api/menu" in matching[0]
    assert "status=200" in matching[0]
    assert "method=GET" in matching[0]
    # The query string (search terms, signed tokens) must never be logged.
    assert "should-not-be-logged" not in "\n".join(lines)


def test_static_files_also_get_the_id(web):
    response = web.get("/static/app.css")
    assert response.status_code == 200
    assert REQUEST_ID_RE.match(response.headers.get("X-Request-ID") or "")


# --------------------------------------------------------------------------- single spinner


def test_loading_pill_yields_to_the_download_chip():
    js = _read("static/app.js")
    # The ownership rule is explicit and idempotent.
    assert "function pillSuppressed" in js
    assert "function syncSpinnerOwnership" in js
    assert "function downloadIndicatorActive" in js
    assert "if (pillSuppressed() || downloadIndicatorActive()) return;" in js
    assert "syncSpinnerOwnership();" in js
    # Exactly one chip/pill element each in the markup.
    html = _read("templates/index.html")
    assert html.count('class="job-chip"') == 1
    assert html.count('class="loading-pill"') == 1
    assert html.count('id="job-chip"') == 1
    assert html.count('id="loading-pill"') == 1
