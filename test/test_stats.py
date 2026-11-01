# -*- coding: utf-8 -*-
"""Owner read-only stats endpoint (1.6.22)."""

import json
import logging
import time

import app as app_module
from core import audit


def _login(client, identity="owner", password="test-password"):
    page = client.get("/login")
    import re
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    return client.post("/login", data={
        "csrf": token, "username": identity, "password": password,
        "next": "/"})


def test_stats_requires_owner_or_widget_token(anon):
    assert anon.get("/api/admin/stats").status_code == 401
    # friends (viewer) may use the player but not the admin feed
    assert _login(anon, "friends", "friends-password").status_code == 302
    assert anon.get("/api/admin/stats").status_code == 403


def test_owner_can_read_stats(web):
    response = web.get("/api/admin/stats")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["updated"] == payload["generated_at"]
    assert payload["window_hours"] == 24
    for key in ("sessions", "sessions_now", "unique_ips_24h", "logins_24h",
                "failures_24h", "lockouts_24h", "bandwidth", "bandwidth_by_day",
                "top_endpoints", "last_ips", "failures_today", "lockouts_today"):
        assert key in payload, key
    assert payload["sessions"]["now"] == payload["sessions_now"] == 1
    assert payload["sessions"]["online_owner"] == 1
    assert payload["sessions"]["online_friends"] == 0
    assert len(payload["sessions"]["items"]) == 1
    item = payload["sessions"]["items"][0]
    assert item["identity"] == "owner"
    assert item["role"] == "admin"
    assert "id" not in item and "session" not in item
    assert item["session_id_masked"].endswith("…")
    with web.session_transaction() as sess:
        raw_sid = sess["sid"]
    assert raw_sid not in json.dumps(payload)
    assert item["session_id_masked"] != raw_sid
    assert payload["bandwidth"]["bytes_in"] == 0
    assert payload["bandwidth"]["window"] == "24h"


def test_widget_token_is_read_only(anon, monkeypatch):
    monkeypatch.setenv("RAVEWEB_WIDGET_TOKEN", "widget-secret")
    response = anon.get("/api/admin/stats",
                        headers={"Authorization": "Bearer widget-secret"})
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert anon.get("/api/admin/stats",
                    headers={"Authorization": "Bearer wrong"}).status_code == 401
    # No mutation surface under the read-only token (and no write routes exist).
    assert anon.post("/api/admin/stats",
                     headers={"Authorization": "Bearer widget-secret"}).status_code == 405
    assert anon.post("/api/admin/sessions/x/revoke",
                     headers={"Authorization": "Bearer widget-secret"}).status_code in (401, 404)
    assert anon.post("/api/admin/blocks",
                     headers={"Authorization": "Bearer widget-secret"}).status_code in (401, 404)


def test_aggregation_counts_and_windows(anon):
    now = time.time()
    old = now - 26 * 3600
    audit.record_event("login_success", "owner", "admin", "10.0.0.1", "ua-a",
                       "/login", 0, 302, ts=now)
    audit.record_event("login_success", "friends", "viewer", "10.0.0.2", "ua-b",
                       "/login", 0, 302, ts=now)
    audit.record_event("login_success", "owner", "admin", "10.0.0.9", "ua-old",
                       "/login", 0, 302, ts=old)  # outside 24h
    audit.record_event("login_failure", "owner", "admin", "10.0.0.1", "ua-a",
                       "/login", 0, 401, ts=now)
    audit.record_event("login_failure", "friends", "viewer", "10.0.0.2", "ua-b",
                       "/login", 0, 401, ts=now)
    audit.record_event("login_locked", "friends", "viewer", "10.0.0.2", "ua-b",
                       "/login", 0, 429, ts=now)
    audit.record_event("request", "owner", "admin", "10.0.0.1", "ua-a",
                       "/api/menu", 1000, 200, ts=now)
    audit.record_event("request", "friends", "viewer", "10.0.0.2", "ua-b",
                       "/api/menu", 2000, 200, ts=now)
    audit.record_event("request", "owner", "admin", "10.0.0.1", "ua-a",
                       "/api/latest", 500, 200, ts=now)
    audit.record_event("request", "owner", "admin", "10.0.0.3", "ua-c",
                       "/api/latest", 999, 200, ts=old)  # outside 24h
    stats = audit.stats()
    assert stats["logins_24h"] == 2
    assert stats["failures_24h"] == 2
    assert stats["lockouts_24h"] == 1
    assert stats["unique_ips_24h"] == 2
    assert stats["requests_24h"] == 3
    assert stats["bytes_24h"] == 3500
    assert stats["top_endpoints"][0] == {
        "endpoint": "/api/menu", "requests": 2, "bytes": 3000}
    assert {item["ip"] for item in stats["last_ips"]} == {"10.0.0.1", "10.0.0.2"}
    # The old event must not appear in the 24h bandwidth/top lists, but it does
    # contribute to the (7-day) daily byte bucket.
    assert stats["bytes_24h"] == 3500
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    today_bucket = [d for d in stats["bandwidth_by_day"] if d["date"] == today][0]
    assert today_bucket["bytes"] == 3500
    assert len(stats["bandwidth_by_day"]) == 7


def test_login_events_and_sessions_are_recorded(anon):
    assert _login(anon, "owner").status_code == 302
    # One successful login + one failed attempt from the same test IP.
    anon.post("/logout")
    page = anon.get("/login")
    import re
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    anon.post("/login", data={"csrf": token, "username": "owner",
                              "password": "wrong-password"})
    stats = audit.stats()
    assert stats["logins_24h"] >= 1
    assert stats["failures_24h"] >= 1
    assert stats["sessions_now"] >= 0


def test_stats_endpoint_reports_live_request_bytes(web):
    before = web.get("/api/admin/stats").get_json()
    web.get("/api/menu")
    after = web.get("/api/admin/stats").get_json()
    assert after["requests_24h"] >= before["requests_24h"] + 1
    endpoints = {item["endpoint"] for item in after["top_endpoints"]}
    assert "/api/menu" in endpoints
    assert after["bandwidth"]["bytes_out"] >= after["bytes_24h"]
