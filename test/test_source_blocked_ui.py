# -*- coding: utf-8 -*-
"""1.6.52: RaveTapePacks failure is a quiet idle state, never a pop-up.

Owner policy for the overhaul: Kodi stays deliberately silent when RTP is
unreachable, and the web shows a non-modal idle line (no toast, no banner, no
"reboot your router" warning).  The background probe still retries once.
See docs/INTEGRATION-3.9.49.md §P0-5.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_rtp_failure_has_no_blocking_banner_or_popup():
    html = _read("templates/index.html")
    css = _read("static/app.css")
    js = _read("static/app.js")
    assert "p-retry" not in html and "Check again" not in html
    assert "p-retry" not in css and ".p-retry" not in css
    assert "RaveTapePacks is temporarily blocking" not in js
    assert "SOURCE_BLOCKED_TEXT" not in js
    # The blocked path must not raise a toast or the offline banner.
    marker = 'if (payload && payload.code === SOURCE_BLOCKED_CODE)'
    blocked = js[js.index(marker):]
    blocked = blocked[:blocked.index("clearBlockedRetry();")]
    assert "toast(" not in blocked
    assert "showBanner(" not in blocked


def test_rtp_failure_shows_a_quiet_idle_line_and_retries_once():
    js = _read("static/app.js")
    assert 'const SOURCE_BLOCKED_CODE = "source_blocked";' in js
    assert 'action: "source_blocked"' in js
    assert "SOURCE_BLOCKED_RETRY_MS = 5000" in js
    assert js.count("setTimeout(sourceBlockedRetry") == 1
    assert "state.blocked.retried" in js
    # The non-modal idle copy is set on both the 403 and DNS/connection paths.
    assert 'const SOURCE_IDLE_TEXT = "Source busy — retrying quietly…";' in js
    assert js.count("setPlayerStatus(SOURCE_IDLE_TEXT);") == 2
