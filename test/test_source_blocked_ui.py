# -*- coding: utf-8 -*-
"""1.6.47 client contract for RaveTapePacks source-blocked errors."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_retry_banner_copy_and_action_are_wired():
    html = _read("templates/index.html")
    js = _read("static/app.js")
    assert 'id="p-retry"' in html and "Check again" in html
    assert 'role="status"' in html
    assert "RaveTapePacks is temporarily blocking this connection" in js
    assert "or reboot your router." in js
    assert "SOURCE_BLOCKED_RETRY_MS = 5000" in js
    assert 'payload.code === SOURCE_BLOCKED_CODE' in js
    assert "probeRaveTapePacks" in js
    # Exactly one scheduled silent retry; manual retries use sourceBlockedRetry.
    assert js.count("setTimeout(sourceBlockedRetry") == 1
    assert "state.blocked.retried" in js
    assert '$("p-retry").addEventListener("click", sourceBlockedRetry)' in js


def test_retry_is_only_used_for_ravetapepacks_urls():
    js = _read("static/app.js")
    assert "isRaveTapePacksUrl" in js
    assert "ravetapepacks.com" in js
    assert 'if (isRaveTapePacksUrl(url)) {' in js
