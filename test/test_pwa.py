# -*- coding: utf-8 -*-
"""PWA / home-screen install tests (web 1.6.19).

Root cause of the "home screen opens an ad page" report: the manifest was fine
on the happy path but the template lacked the iOS standalone meta tags (so
Safari saved a bookmark to the current tab instead of the app start URL) and
there was no service worker/app-shell fallback.  These tests lock the manifest
identity, the apple tags, the root-scoped worker and its same-origin navigation
fallback.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_manifest_has_a_root_identity_and_standalone_display(web):
    response = web.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/manifest+json")
    assert response.headers["Cache-Control"] == "no-cache"
    manifest = response.get_json()
    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["display_override"][0] == "standalone"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"180x180", "512x512"} <= sizes
    assert all(icon["src"].startswith("/static/art/") for icon in manifest["icons"])


def test_index_has_the_ios_standalone_meta_tags(web):
    body = web.get("/").get_data(as_text=True)
    for marker in (
        'rel="manifest"',
        'name="apple-mobile-web-app-capable" content="yes"',
        'name="mobile-web-app-capable" content="yes"',
        'name="apple-mobile-web-app-title" content="Rave Player"',
        'name="apple-mobile-web-app-status-bar-style"',
        'rel="apple-touch-icon"',
        'src="/static/sw-register.js',
    ):
        assert marker in body, marker
    # Registration is an external script (CSP script-src 'self', no inline JS).
    assert "navigator.serviceWorker.register" not in body


def test_service_worker_is_served_at_root_scope(web):
    response = web.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/javascript")
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert response.headers["Cache-Control"] == "no-cache"
    body = response.get_data(as_text=True)
    assert "caches.match" in body or "caches.open" in body


def test_service_worker_only_falls_back_for_same_origin_navigations():
    worker = _read("static/sw.js")
    # Foreign pages/ads are never handled or cached.
    assert "url.origin !== self.location.origin" in worker
    # Live API and downloads stay network-only (no stale library/data).
    assert 'url.pathname.startsWith("/api/")' in worker
    assert 'request.method !== "GET"' in worker
    # A navigation gets the cached app shell when the network fails/404s.
    assert 'request.mode === "navigate"' in worker
    assert 'caches.match(SHELL' in worker
    assert 'const SHELL = "/";' in worker
    # Static assets are versioned via ?v= in the template, so a deploy does not
    # serve yesterday's JS from the cache.
    assert "ignoreSearch" in worker


def test_service_worker_registration_is_secure_context_only(web):
    script = web.get("/static/sw-register.js").get_data(as_text=True)
    assert "window.isSecureContext" in script
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/" })' in script
    assert "if (!(\"serviceWorker\" in navigator)) return;" in script
