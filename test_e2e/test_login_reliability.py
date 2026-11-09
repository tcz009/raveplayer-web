# -*- coding: utf-8 -*-
"""Real-browser login reliability (BUG 1).

Fresh context -> GET /login -> owner/password -> submit, repeated in the same
context after logout; a stale server-side session cookie; and the duplicate
POST a double-click produces.  The fresh-page first POST must always succeed,
and the stale/duplicate paths must never consume a login attempt.

Run with ``pytest test_e2e`` and a Playwright Chromium/Chrome install.
"""

import sqlite3
import time

import app as app_module
from core import audit


def _fill_login(page, base_url, username="owner", password="test-password"):
    page.goto(base_url + "/login", wait_until="domcontentloaded")
    assert page.locator("#login-form").count() == 1
    page.fill("#username", username)
    page.fill("#password", password)


def _submit_and_wait(page, base_url):
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/login")) as info:
        page.click("#login-submit")
    response = info.value
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_function("() => !document.querySelector('#login-form')")
    assert "/login" not in page.url
    assert response.status == 302
    assert response.headers.get("cache-control") == "no-store"
    return response


def _cookie(page):
    for cookie in page.context.cookies():
        if cookie["name"] == app_module.app.config["SESSION_COOKIE_NAME"]:
            return cookie["value"]
    return ""


def _wait_for_cookie(page):
    """Wait for the browser to commit the response's session cookie.

    Chrome can finish DOMContentLoaded before the Set-Cookie is visible in the
    context jar when a service worker is activating; a real user spends longer
    than this typing, but the test must not race the jar.
    """
    deadline = time.time() + 10
    while time.time() < deadline:
        if _cookie(page):
            return _cookie(page)
        page.wait_for_timeout(50)
    raise AssertionError("session cookie was not stored")


def _login_event_counts():
    with sqlite3.connect(audit.db_path()) as conn:
        return dict(conn.execute(
            "SELECT event, COUNT(*) FROM events WHERE event IN "
            "('login_failure','login_csrf_rejected') GROUP BY event"))


def test_first_login_post_from_a_fresh_page_succeeds(live_server, chromium):
    context = chromium.new_context()
    try:
        page = context.new_page()
        _fill_login(page, live_server.url)
        _submit_and_wait(page, live_server.url)
        _wait_for_cookie(page)

        whoami = context.request.get(live_server.url + "/api/admin/whoami")
        assert whoami.ok
        assert whoami.json() == {"ok": True, "identity": "owner", "role": "admin"}

        # Same context, second and third sign-in after an explicit logout.
        for _ in range(2):
            assert context.request.post(live_server.url + "/logout").ok
            _fill_login(page, live_server.url)
            _submit_and_wait(page, live_server.url)
            _wait_for_cookie(page)
            assert context.request.get(
                live_server.url + "/api/admin/whoami").json()["identity"] == "owner"
    finally:
        context.close()


def test_stale_session_cookie_shows_a_fresh_form_and_logs_in(live_server, chromium):
    context = chromium.new_context()
    try:
        page = context.new_page()
        _fill_login(page, live_server.url)
        _submit_and_wait(page, live_server.url)
        _wait_for_cookie(page)

        # Revoke the server-side session but keep the signed browser cookie:
        # this is the post-restart / restored-backup case, without a new secret.
        cookie = _cookie(page)
        serializer = app_module.app.session_interface.get_signing_serializer(
            app_module.app)
        data = serializer.loads(cookie)
        audit.revoke_session(data["sid"])
        before = _login_event_counts()

        page.goto(live_server.url + "/login", wait_until="domcontentloaded")
        assert page.locator("#login-form").count() == 1
        assert page.locator(".door-error").count() == 0
        # With a service worker active this navigation must still commit the
        # fresh CSRF-only cookie (a respondWith-returned /login dropped it).
        _wait_for_cookie(page)
        page.wait_for_timeout(200)
        token = page.locator('input[name="csrf"]').get_attribute("value")
        assert token

        page.fill("#username", "owner")
        page.fill("#password", "test-password")
        _submit_and_wait(page, live_server.url)
        assert context.request.get(
            live_server.url + "/api/admin/whoami").json()["identity"] == "owner"

        # The stale-session GET/POST pair must not record a failure attempt.
        assert _login_event_counts() == before
    finally:
        context.close()


def test_duplicate_login_fetch_does_not_show_a_csrf_error(live_server, chromium):
    context = chromium.new_context()
    try:
        page = context.new_page()
        page.goto(live_server.url + "/login", wait_until="domcontentloaded")
        before = _login_event_counts()
        token = page.locator('input[name="csrf"]').get_attribute("value")
        result = page.evaluate(
            """async (token) => {
                async function post(csrf) {
                    const body = new FormData();
                    body.append("csrf", csrf);
                    body.append("username", "owner");
                    body.append("password", "test-password");
                    body.append("next", "/");
                    const response = await fetch("/login", {
                        method: "POST", body, credentials: "same-origin",
                    });
                    const text = await response.text();
                    return {status: response.status, url: response.url,
                            error: text.includes("session expired")};
                }
                return [await post(token), await post(token)];
            }""",
            token,
        )
        assert result[0]["status"] == 200 and not result[0]["error"]
        assert result[1]["status"] == 200 and not result[1]["error"]
        assert result[1]["url"].rstrip("/") == live_server.url
        assert _login_event_counts() == before
    finally:
        context.close()
