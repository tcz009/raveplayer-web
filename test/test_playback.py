# -*- coding: utf-8 -*-
"""Owner-only playback presence/history/export (1.6.34)."""

import json
import re
import time

import app as app_module
from core import audit, playback

CSRF = {"X-CSRF-Token": "test-csrf"}


def _client():
    return app_module.app.test_client()


def _login(client, identity="friends", password="friends-password"):
    page = client.get("/login")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    return client.post("/login", data={
        "csrf": token, "username": identity, "password": password, "next": "/"})


def _heartbeat(client, state="playing", **fields):
    payload = {"state": state, "label": "Boot Track", "source": "archive",
               "position": 12.5, "duration": 187, "client_ts": time.time()}
    payload.update(fields)
    return client.post("/api/playback", json=payload)


def test_heartbeat_requires_auth_and_validates(anon, web):
    assert anon.post("/api/playback", json={"state": "playing"}).status_code == 401
    assert _heartbeat(web).status_code == 204
    assert _heartbeat(web, state="bogus").status_code == 400
    assert _heartbeat(web, label="x" * 301).status_code == 400
    assert _heartbeat(web, source="x" * 65).status_code == 400
    assert _heartbeat(web, position="NaN").status_code == 400
    assert _heartbeat(web, duration=-5).status_code == 400
    assert _heartbeat(web, position=10 ** 9).status_code == 400
    assert _heartbeat(web, label="https://archive.org/secret.mp3").status_code == 400


def test_heartbeat_rate_limit(web):
    codes = [_heartbeat(web).status_code for _ in range(app_module.PLAYBACK_RATE_LIMIT)]
    assert codes == [204] * len(codes)
    assert _heartbeat(web).status_code == 429


def test_owner_sees_both_identities_and_friends_cannot(web):
    friend = _client()
    assert _login(friend).status_code == 302
    assert _heartbeat(web, state="playing", label="Owner track").status_code == 204
    assert _heartbeat(friend, state="paused", label="Friend track",
                      source="hardscore").status_code == 204
    response = web.get("/api/admin/playback")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["stale_after_seconds"] == 90
    rows = {row["identity"]: row for row in payload["now"]}
    assert set(rows) == {"owner", "friends"}
    assert rows["owner"]["state"] == "playing"
    assert rows["owner"]["label"] == "Owner track"
    assert rows["friends"]["state"] == "paused"
    assert rows["friends"]["source"] == "hardscore"
    assert all(row["session_id_masked"].endswith("…") for row in rows.values())
    assert all("url" not in row for row in rows.values())
    # Friends are denied the owner view, history and export.
    assert friend.get("/api/admin/playback").status_code == 403
    assert friend.get("/api/admin/playback/history").status_code == 403
    assert friend.get("/api/admin/playback/export").status_code == 403
    # The masked (not raw) session id is what the owner sees.
    for row in rows.values():
        assert len(row["session_id_masked"]) == 11


def test_stale_heartbeat_is_presented_as_stopped(web):
    assert _heartbeat(web, state="playing").status_code == 204
    with audit._connect() as conn:
        conn.execute("UPDATE playback_now SET updated=?",
                     (time.time() - playback.STALE_AFTER - 5,))
        conn.commit()
    row = web.get("/api/admin/playback").get_json()["now"][0]
    assert row["state"] == "stopped"
    assert row["online"] is False
    assert row["age_seconds"] > playback.STALE_AFTER


def test_history_window_identity_filter_and_audit(web):
    friend = _client()
    assert _login(friend).status_code == 302
    now = time.time()
    playback.record("owner", "owner-sid", "admin", "playing", "Owner old",
                    "archive", 1, 100, now=now - 2 * 3600)
    playback.record("owner", "owner-sid", "admin", "paused", "Owner recent",
                    "archive", 2, 100, now=now - 60)
    playback.record("friends", "friend-sid", "viewer", "playing", "Friend recent",
                    "hardscore", 3, 100, now=now - 30)
    recent = web.get("/api/admin/playback/history?hours=1").get_json()["items"]
    assert [row["label"] for row in recent] == ["Friend recent", "Owner recent"]
    old = web.get("/api/admin/playback/history?hours=4").get_json()["items"]
    assert "Owner old" in [row["label"] for row in old]
    only = web.get("/api/admin/playback/history?hours=4&identity=owner").get_json()["items"]
    assert {row["identity"] for row in only} == {"owner"}
    assert web.get("/api/admin/playback/history?hours=0").status_code == 400
    assert web.get("/api/admin/playback/history?identity=nobody").status_code == 400
    for row in recent:
        assert row["event"] == "play_event"
        assert row["session_id_masked"].endswith("…")
        assert "http" not in json.dumps(row)
    with audit._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event='play_event'").fetchone()["n"]
    assert count >= 3


def test_export_is_json_download_without_secrets(web):
    assert _heartbeat(web).status_code == 204
    response = web.get("/api/admin/playback/export?hours=24")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.headers["Content-Disposition"].startswith(
        'attachment; filename="rave-playback-')
    payload = response.get_json()
    assert payload["kind"] == "rave-playback-log"
    assert payload["hours"] == 24
    assert payload["stale_after_seconds"] == 90
    assert payload["now"] and payload["history"]
    text = response.get_data(as_text=True)
    assert '"url"' not in text and "http://" not in text and "https://" not in text
    assert web.get("/api/admin/playback/export?hours=999").status_code == 400
