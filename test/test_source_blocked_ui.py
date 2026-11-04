# -*- coding: utf-8 -*-
"""1.6.50: RaveTapePacks failures are silent like the Kodi client."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_rtp_failure_has_no_visible_banner_or_retry_action():
    html = _read("templates/index.html")
    css = _read("static/app.css")
    js = _read("static/app.js")
    assert "p-retry" not in html and "Check again" not in html
    assert "p-retry" not in css and ".p-retry" not in css
    assert "RaveTapePacks is temporarily blocking" not in js
    assert "SOURCE_BLOCKED_TEXT" not in js


def test_rtp_failure_logs_debug_only_and_retries_once():
    js = _read("static/app.js")
    assert 'const SOURCE_BLOCKED_CODE = "source_blocked";' in js
    assert 'action: "source_blocked"' in js
    assert "SOURCE_BLOCKED_RETRY_MS = 5000" in js
    assert js.count("setTimeout(sourceBlockedRetry") == 1
    assert "state.blocked.retried" in js
    # The blocked paths clear the visible status rather than display a banner.
    assert js.count('setPlayerStatus("");') >= 2
