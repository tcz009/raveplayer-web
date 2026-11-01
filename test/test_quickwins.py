# -*- coding: utf-8 -*-
"""1.6.24 quick wins: shared login limiter, themed errors, CSP reporting."""

import logging
import os
import subprocess
import sys

import pytest

import app as app_module
from core import audit


def _explode():
    raise RuntimeError("test explosion")


if "test_500" not in app_module.app.view_functions:
    app_module.app.add_url_rule("/__test_500", "test_500", _explode)


def test_login_lockout_is_shared_across_processes(anon):
    """One worker records the failures; the parent's limiter sees the lock."""
    db_path = audit.db_path()
    code = (
        "from core import audit\n"
        "audit.configure(%r)\n"
        "for _ in range(5):\n"
        "    audit.login_fail('ip\\0owner', 5, 600, 900)\n"
        "print(audit.login_retry_after('ip\\0owner', 600, 900))\n" % db_path
    )
    env = dict(os.environ)
    env["RAVEWEB_AUTH_DB"] = db_path
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0
    # Same shared DB, different SqliteLoginLimiter instance (another worker).
    other = app_module.auth.SqliteLoginLimiter(5, 600, 900)
    assert other.retry_after("ip\0owner") > 0
    other.reset("ip\0owner")
    assert other.retry_after("ip\0owner") == 0


def test_themed_404_and_api_json_404(web):
    page = web.get("/no-such-reel")
    assert page.status_code == 404
    text = page.get_data(as_text=True)
    assert "LOST THE TAPE" in text
    assert 'href="/"' in text
    assert "auth.css" in text
    api = web.get("/api/no-such-reel")
    assert api.status_code == 404
    assert api.is_json and api.get_json()["error"] == "not found"


def test_themed_500_and_api_json_500(web):
    page = web.get("/__test_500")
    assert page.status_code == 500
    assert "THE SIGNAL DROPPED" in page.get_data(as_text=True)
    api = web.get("/api/no-such-reel")
    assert api.status_code == 404 and api.is_json


def test_csp_report_only_header_and_report_endpoint(anon, caplog):
    page = anon.get("/login")
    assert "Content-Security-Policy" in page.headers
    report_only = page.headers.get("Content-Security-Policy-Report-Only", "")
    assert "report-uri /api/csp-report" in report_only
    assert 'report-to csp-endpoint' in report_only
    assert "csp-endpoint" in page.headers.get("Report-To", "")
    with caplog.at_level(logging.WARNING, logger="app"):
        response = anon.post("/api/csp-report", json={
            "csp-report": {"effective-directive": "script-src-elem",
                           "blocked-uri": "https://ads.example/x.js",
                           "document-uri": "http://127.0.0.1/",
                           "disposition": "report"}})
    assert response.status_code == 204
    assert "csp violation directive=script-src-elem" in caplog.text
    # Public endpoint; bad/oversized report bodies never 500.
    assert anon.post("/api/csp-report", data=b"not json",
                     content_type="application/csp-report").status_code == 204
    huge = b"{" + b"x" * (70 * 1024) + b"}"
    assert anon.post("/api/csp-report", data=huge,
                     content_type="application/csp-report").status_code == 413


def test_report_only_can_be_disabled(monkeypatch):
    monkeypatch.setattr(app_module, "CSP_REPORT_ONLY", False)
    # The header is built in the after_request hook from the current config.
    with app_module.app.test_client() as client:
        response = client.get("/login")
    assert "Content-Security-Policy-Report-Only" not in response.headers
