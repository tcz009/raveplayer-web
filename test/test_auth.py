# -*- coding: utf-8 -*-
"""Single-operator login tests (1.6.20).

The shared ``web`` fixture is pre-authenticated so the existing API tests stay
focused; these tests use the fresh ``anon`` client to exercise the real login
flow, CSRF, throttling, cookies and the guard on every route class.
"""

import logging
import re
import sqlite3
import time

import pytest

import app as app_module
from core import audit, auth


def csrf_from(client, path="/login"):
    page = client.get(path)
    match = re.search(rb'name="csrf" value="([^"]+)"', page.data)
    assert match, "login page has no CSRF token"
    return match.group(1).decode()


def login(client, password="test-password", remember=False, next_url="/", csrf=None,
          username="owner"):
    token = csrf or csrf_from(client)
    data = {"csrf": token, "username": username, "password": password, "next": next_url}
    if remember:
        data["remember"] = "1"
    return client.post("/login", data=data)


def test_api_is_json_401_for_every_unauth_route(anon):
    for path in ("/api/menu", "/api/latest", "/api/surprise", "/api/downloads",
                 "/api/stream?url=https://example.com/a.mp3",
                 "/api/download/pack-0"):
        response = anon.get(path)
        assert response.status_code == 401, path
        assert response.is_json
        assert response.get_json()["error"] == "authentication required"


def test_pages_redirect_to_login_with_next(anon):
    response = anon.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=")
    assert "%2F" in response.headers["Location"] or "/" in response.headers["Location"]
    response = anon.get("/favs?x=1")
    assert response.status_code == 302
    assert "/login?next=" in response.headers["Location"]


def test_public_surface_is_narrow(anon):
    for path in ("/login", "/healthz", "/manifest.webmanifest", "/sw.js",
                 "/static/auth.css", "/static/login.js",
                 "/static/art/icon.png", "/static/art/icon-180.png"):
        assert anon.get(path).status_code == 200, path
    for path in ("/static/app.js", "/static/app.css", "/static/art/hero.jpg"):
        assert anon.get(path).status_code == 401, path


def test_login_page_is_designed_and_has_csrf(anon):
    response = anon.get("/login")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="login-form"' in body
    assert "THE DOOR" in body
    assert 'name="csrf"' in body
    assert '<script src="/static/login.js' in body
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_login_requires_csrf(anon, caplog):
    with caplog.at_level(logging.WARNING, logger="app"):
        response = anon.post("/login", data={"password": "test-password"})
    assert response.status_code == 400
    assert b"session expired" in response.data.lower()
    response = anon.post("/login", data={"csrf": "nope", "password": "test-password"})
    assert response.status_code == 400
    assert "csrf rejected" in caplog.text


def test_login_success_sets_hardened_cookie_and_audits(anon, caplog):
    with caplog.at_level(logging.INFO, logger="app"):
        response = login(anon, next_url="/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    cookie = "; ".join(response.headers.getlist("Set-Cookie"))
    assert "raveplayer_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie  # plain HTTP must keep working
    assert "auth: login success" in caplog.text
    assert "test-password" not in caplog.text
    assert anon.get("/api/menu").status_code == 200
    with anon.session_transaction() as sess:
        assert sess["identity"] == "owner"
        assert sess["role"] == "admin"


def test_remember_me_extends_cookie_and_session(anon):
    response = login(anon, remember=True)
    cookie = "; ".join(response.headers.getlist("Set-Cookie"))
    assert "Expires=" in cookie or "Max-Age=" in cookie
    with anon.session_transaction() as sess:
        assert sess["remember"] is True


def test_failed_login_is_generic_and_throttled(anon, caplog):
    with caplog.at_level(logging.WARNING, logger="app"):
        for _ in range(app_module.AUTH.max_failures):
            response = login(anon, password="definitely-wrong")
            assert response.status_code == 401
            assert b"Invalid username or password." in response.data
        locked = login(anon, password="definitely-wrong")
    assert locked.status_code == 429
    assert b"Too many attempts" in locked.data
    assert locked.headers["Retry-After"]
    # Even the correct password is refused while the IP is locked out.
    assert login(anon, password="test-password").status_code == 429
    assert "login failure" in caplog.text
    assert "definitely-wrong" not in caplog.text


def test_logout_clears_the_session(anon):
    assert login(anon).status_code == 302
    assert anon.get("/api/menu").status_code == 200
    response = anon.post("/logout")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert "raveplayer_session=;" in "; ".join(response.headers.getlist("Set-Cookie"))
    assert anon.get("/api/menu").status_code == 401


def test_session_expiry_and_remember_window(anon):
    login(anon)
    with anon.session_transaction() as sess:
        sess["login_at"] = time.time() - (app_module.AUTH.session_hours * 3600 + 30)
    assert anon.get("/api/menu").status_code == 401

    login(anon, remember=True)
    with anon.session_transaction() as sess:
        sess["login_at"] = time.time() - (app_module.AUTH.session_hours * 3600 + 30)
    assert anon.get("/api/menu").status_code == 200  # remember window still valid
    with anon.session_transaction() as sess:
        sess["login_at"] = time.time() - (app_module.AUTH.remember_days * 86400 + 30)
    assert anon.get("/api/menu").status_code == 401


def test_https_marks_cookie_secure_and_sends_hsts(anon):
    page = anon.get("/login", base_url="https://localhost")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    response = anon.post("/login", base_url="https://localhost",
                         data={"csrf": token, "username": "owner",
                               "password": "test-password"})
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")
    cookie = "; ".join(response.headers.getlist("Set-Cookie"))
    assert "Secure" in cookie
    assert anon.get("/api/menu", base_url="https://localhost").status_code == 200


def test_owner_and_friends_logins_have_distinct_roles(anon):
    # friends (viewer) can play everything non-admin.
    token = csrf_from(anon)
    response = anon.post("/login", data={"csrf": token, "username": "friends",
                                         "password": "friends-password"})
    assert response.status_code == 302
    with anon.session_transaction() as sess:
        assert sess["identity"] == "friends"
        assert sess["role"] == "viewer"
    assert anon.get("/api/menu").status_code == 200
    assert anon.get("/api/admin/whoami").status_code == 403
    anon.post("/logout")

    # owner (admin) gets the admin probe.
    response = login(anon, username="owner")
    assert response.status_code == 302
    assert anon.get("/api/admin/whoami").get_json() == {
        "ok": True, "identity": "owner", "role": "admin"}


def test_wrong_identity_password_pair_is_rejected_generically(anon):
    for username, password in (("owner", "friends-password"),
                               ("friends", "test-password"),
                               ("nobody", "test-password")):
        response = login(anon, username=username, password=password)
        assert response.status_code == 401
        assert b"Invalid username or password." in response.data


def test_failed_logins_are_locked_out_per_identity_and_ip(anon):
    # Lock friends only; the owner identity from the same IP must still work.
    for _ in range(app_module.AUTH.max_failures):
        assert login(anon, username="friends", password="wrong-friends").status_code == 401
    assert login(anon, username="friends", password="friends-password").status_code == 429
    owner = login(anon, username="owner")
    assert owner.status_code == 302
    anon.post("/logout")
    assert login(anon, username="friends", password="friends-password").status_code == 429


def test_friends_hash_can_rotate_independently(anon, monkeypatch):
    monkeypatch.setattr(app_module.AUTH, "friends_hash",
                        auth.generate_hash("new-friends-password"))
    assert login(anon, username="friends", password="friends-password").status_code == 401
    assert login(anon, username="friends", password="new-friends-password").status_code == 302
    # The owner credential is untouched by a friends-only rotation.
    anon.post("/logout")
    assert login(anon, username="owner").status_code == 302


def test_open_redirect_targets_are_sanitised(anon):
    token = csrf_from(anon)
    response = anon.post("/login", data={"csrf": token, "username": "owner",
                                         "password": "test-password",
                                         "next": "https://evil.example/"})
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    anon.post("/logout")
    token = csrf_from(anon)
    response = anon.post("/login", data={"csrf": token, "username": "owner",
                                         "password": "test-password",
                                         "next": "//evil.example/"})
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_auth_config_fails_closed_and_supports_first_run(monkeypatch):
    with pytest.raises(auth.AuthConfigError):
        auth.load_config({"SECRET_KEY": "x"})  # no password
    with pytest.raises(auth.AuthConfigError):
        auth.load_config({"RAVEWEB_PASSWORD": "long-enough",
                          "SECRET_KEY": "secret", "RAVEWEB_ENV": "production"})
    with pytest.raises(auth.AuthConfigError):
        auth.load_config({"RAVEWEB_PASSWORD": "long-enough",
                          "RAVEWEB_ENV": "production"})
    config = auth.load_config({"RAVEWEB_PASSWORD": "test-password",
                               "RAVEWEB_FRIENDS_PASSWORD": "friends-password",
                               "SECRET_KEY": "test-secret", "RAVEWEB_ENV": "test"})
    assert config.generated_hash
    assert config.generated_friends_hash
    assert auth.verify_password(config.password_hash, "test-password")
    assert auth.verify_password(config.friends_hash, "friends-password")
    rotated = auth.load_config({"RAVEWEB_PASSWORD": "test-password",
                                "RAVEWEB_FRIENDS_PASSWORD": "rotated-friends",
                                "SECRET_KEY": "test-secret", "RAVEWEB_ENV": "test"})
    assert not auth.verify_password(rotated.friends_hash, "friends-password")
    assert auth.verify_password(rotated.friends_hash, "rotated-friends")
    custom = auth.generate_hash("another-password")
    assert auth.hash_scheme(custom) in ("argon2", "bcrypt", "werkzeug")
    assert auth.verify_password(custom, "another-password")
    assert not auth.verify_password(custom, "wrong-password")
    with pytest.raises(auth.AuthConfigError):
        auth.validate_scheme("plaintext-not-a-hash")


def test_session_config_is_cookie_safe():
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app_module.app.config["SESSION_COOKIE_NAME"] == "raveplayer_session"
    assert app_module.AUTH.password_hash


# --------------------------------------------------------------------------- login reliability (BUG 1)


def _login_events(name):
    path = audit.db_path()
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT identity, detail FROM events WHERE event=? ORDER BY id", (name,)
        ).fetchall()


def _login_data(token, username="owner", password="test-password", next_url="/"):
    return {"csrf": token, "username": username, "password": password,
            "next": next_url}


def test_duplicate_login_post_after_success_redirects_instead_of_csrf_error(anon):
    """Double-click / second Enter: the duplicate must not paint a 400 page.

    The first POST authenticates and rotates the session CSRF.  A duplicate
    carrying the pre-rotation token and the new session cookie used to fail
    CSRF (login_csrf_rejected) even though the user was signed in.
    """
    token = csrf_from(anon)
    data = _login_data(token)
    first = anon.post("/login", data=data)
    assert first.status_code == 302
    second = anon.post("/login", data=data)  # same stale form token
    assert second.status_code == 302
    assert second.headers["Location"] == "/"
    assert second.headers["Cache-Control"] == "no-store"
    assert anon.get("/api/admin/whoami").get_json() == {
        "ok": True, "identity": "owner", "role": "admin"}
    assert _login_events("login_csrf_rejected") == []
    assert app_module.LOGIN_LIMITER.retry_after("127.0.0.1\x00owner") == 0


def test_stale_session_cookie_is_cleared_and_does_not_consume_an_attempt(anon):
    """A signed cookie whose server-side session is gone is replaced, not fatal."""
    assert login(anon).status_code == 302
    with anon.session_transaction() as sess:
        sid = sess["sid"]
    audit.revoke_session(sid)

    page = anon.get("/login")
    assert page.status_code == 200
    with anon.session_transaction() as sess:
        assert sess.get("auth") is None
        assert sess.get("sid") is None
        assert sess.get("identity") is None
        assert sess.get("csrf")
        fresh = sess["csrf"]

    response = anon.post("/login", data=_login_data(fresh))
    assert response.status_code == 302
    assert anon.get("/api/menu").status_code == 200
    assert _login_events("login_failure") == []
    assert app_module.LOGIN_LIMITER.retry_after("127.0.0.1\x00owner") == 0


def test_stale_csrf_is_audited_with_reason_and_never_counts_as_a_failure(anon):
    csrf_from(anon)
    with anon.session_transaction() as sess:
        sess["csrf"] = "rotated-token"  # the page the user has is now stale

    response = anon.post("/login", data=_login_data("old-token"))
    assert response.status_code == 400
    rows = _login_events("login_csrf_rejected")
    assert rows and rows[-1] == ("owner", "reason=mismatch")
    assert _login_events("login_failure") == []
    assert app_module.LOGIN_LIMITER.retry_after("127.0.0.1\x00owner") == 0

    # The 400 page carries the current token, so one retry succeeds.
    fresh = re.search(rb'name="csrf" value="([^"]+)"', response.data).group(1).decode()
    assert anon.post("/login", data=_login_data(fresh)).status_code == 302


def test_owner_alias_admin_is_accepted_and_normalised(anon):
    """The legacy owner autofill username maps to the same owner account."""
    response = login(anon, username="admin")
    assert response.status_code == 302
    with anon.session_transaction() as sess:
        assert sess["identity"] == "owner"
        assert sess["role"] == "admin"
    assert anon.get("/api/admin/whoami").get_json()["identity"] == "owner"
    assert _login_events("login_success")[-1] == ("owner", "remember=0;alias=admin")

    anon.post("/logout")
    bad = login(anon, username="admin", password="wrong-password")
    assert bad.status_code == 401
    assert b"Invalid username or password." in bad.data
    # The canonical owner identity owns the failure counter (no alias bypass).
    assert _login_events("login_failure")[-1][0] == "owner"
    with sqlite3.connect(audit.db_path()) as conn:
        keys = [row[0] for row in conn.execute("SELECT key FROM login_failures")]
    assert keys == ["127.0.0.1\x00owner"]


def test_login_survives_a_broken_audit_store(anon, monkeypatch, caplog):
    """Recording the login event is best-effort; auth itself must still succeed."""
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(audit, "record_event", boom)
    with caplog.at_level(logging.ERROR, logger="app"):
        response = login(anon)
    assert response.status_code == 302
    assert anon.get("/api/menu").status_code == 200
    assert "could not record login_success" in caplog.text


def test_login_get_preserves_anonymous_csrf_but_rotates_a_stale_auth_cookie(anon):
    first = csrf_from(anon)
    assert csrf_from(anon) == first  # two tabs keep one form token

    with anon.session_transaction() as sess:
        sess["auth"] = True
        sess["identity"] = "owner"
        sess["role"] = "admin"
        sess["sid"] = "not-a-real-session"
        sess["login_at"] = time.time()
    rotated = csrf_from(anon)
    assert rotated != first
    with anon.session_transaction() as sess:
        assert sess.get("auth") is None
        assert sess.get("csrf") == rotated
