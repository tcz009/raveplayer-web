# -*- coding: utf-8 -*-
"""Phase-2 owner write API: sessions, blocks, friends enable/disable."""

import re
import time

import app as app_module
from core import audit

CSRF = {"X-CSRF-Token": "test-csrf"}


def _client():
    return app_module.app.test_client()


def _login(client, identity="friends", password="friends-password"):
    page = client.get("/login")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    return client.post("/login", data={
        "csrf": token, "username": identity, "password": password, "next": "/"})


def _masked_for(owner, identity):
    listing = owner.get("/api/admin/sessions").get_json()["sessions"]
    return next(row["session_id_masked"] for row in listing if row["identity"] == identity)


def test_sessions_are_masked_and_revoke_only_the_selected_session(web):
    friend = _client()
    assert _login(friend).status_code == 302
    friend_mask = _masked_for(web, "friends")
    owner_mask = _masked_for(web, "owner")
    assert friend_mask.endswith("…") and owner_mask.endswith("…")
    payload = web.get("/api/admin/sessions").get_json()
    assert all("session_id_masked" in row and "id" not in row
               for row in payload["sessions"])
    with friend.session_transaction() as sess:
        raw_sid = sess["sid"]
    assert raw_sid not in str(payload)
    # Raw ids are not accepted by the route.
    assert web.post("/api/admin/sessions/%s/revoke" % raw_sid,
                    headers=CSRF).status_code == 404
    response = web.post("/api/admin/sessions/%s/revoke" % friend_mask, headers=CSRF)
    assert response.status_code == 200
    assert response.get_json()["revoked"] is True
    assert friend.get("/api/menu").status_code == 401
    assert web.get("/api/menu").status_code == 200
    assert friend_mask not in [row["session_id_masked"]
                               for row in web.get("/api/admin/sessions").get_json()["sessions"]]


def test_admin_writes_require_csrf_and_owner(web):
    friend = _client()
    assert _login(friend).status_code == 302
    # Find a friend session mask by logging one in first, then attempt as friend.
    friend_mask = _masked_for(web, "friends")
    assert web.post("/api/admin/sessions/%s/revoke" % friend_mask).status_code == 403
    assert friend.post("/api/admin/sessions/%s/revoke" % friend_mask,
                       headers=CSRF).status_code == 403
    assert web.post("/api/admin/blocks", json={"kind": "identity", "value": "friends"}).status_code == 403
    assert web.post("/api/admin/friends/disable", headers=CSRF).status_code == 200
    assert web.post("/api/admin/friends/enable", headers=CSRF).status_code == 200


def test_identity_block_denies_existing_and_next_login(web):
    friend = _client()
    assert _login(friend).status_code == 302
    friend_mask = _masked_for(web, "friends")
    assert friend_mask
    response = web.post("/api/admin/blocks", headers=CSRF, json={
        "kind": "identity", "value": "friends", "reason": "test",
        "ttl_seconds": 600})
    assert response.status_code == 201
    block = response.get_json()["block"]
    assert block["kind"] == "identity" and block["value"] == "friends"
    assert friend.get("/api/menu").status_code == 401
    # Fresh login with the right password is still blocked (generic copy).
    retry = _client()
    page = retry.get("/login")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    blocked = retry.post("/login", data={"csrf": token, "username": "friends",
                                         "password": "friends-password"})
    assert blocked.status_code == 403
    assert b"Access temporarily blocked." in blocked.data
    # The owner is unaffected and can list/delete the block.
    assert web.get("/api/menu").status_code == 200
    listing = web.get("/api/admin/blocks").get_json()["blocks"]
    assert any(row["id"] == block["id"] for row in listing)
    assert web.delete("/api/admin/blocks/%s" % block["id"], headers=CSRF).status_code == 200
    friend2 = _client()
    assert _login(friend2).status_code == 302
    assert friend2.get("/api/menu").status_code == 200


def test_ip_block_denies_login_from_that_ip(web):
    blocked_ip = "10.9.9.9"
    response = web.post("/api/admin/blocks", headers=CSRF, json={
        "kind": "ip", "value": blocked_ip, "ttl_seconds": 600})
    assert response.status_code == 201
    client = _client()
    page = client.get("/login")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    denied = client.post("/login", environ_base={"REMOTE_ADDR": blocked_ip},
                         data={"csrf": token, "username": "owner",
                               "password": "test-password"})
    assert denied.status_code == 403
    # clean up
    block_id = response.get_json()["block"]["id"]
    assert web.delete("/api/admin/blocks/%s" % block_id, headers=CSRF).status_code == 200


def test_friends_disable_is_runtime_and_audited(web):
    friend = _client()
    assert _login(friend).status_code == 302
    assert web.post("/api/admin/friends/disable", headers=CSRF).status_code == 200
    assert friend.get("/api/menu").status_code == 401
    retry = _client()
    # Disabled friends cannot log in; owner still can.
    assert _login(retry).status_code == 401
    owner = _client()
    assert _login(owner, "owner", "test-password").status_code == 302
    assert web.post("/api/admin/friends/enable", headers=CSRF).status_code == 200
    again = _client()
    assert _login(again).status_code == 302
    stats = audit.stats()
    events = [row["event"] for row in stats["recent_actions"]]
    assert "admin_friends_disabled" in events and "admin_friends_enabled" in events
    # owner block/revoke actions are in the same recent-action feed
    assert stats["blocked_now"] == 0


def test_admin_action_surface_rejects_friends(web):
    friend = _client()
    assert _login(friend).status_code == 302
    for method, path, kwargs in (
            ("get", "/api/admin/sessions", {}),
            ("get", "/api/admin/blocks", {}),
            ("get", "/api/admin/csrf", {}),
            ("post", "/api/admin/friends/disable", {}),
            ("post", "/api/admin/blocks", {"json": {"kind": "identity", "value": "owner"}})):
        response = getattr(friend, method)(path, headers=CSRF, **kwargs)
        assert response.status_code == 403, (path, response.status_code)


def test_admin_actions_are_rate_limited(web):
    statuses = []
    for index in range(31):
        response = web.post("/api/admin/blocks", headers=CSRF, json={
            "kind": "identity", "value": "spam-%d" % index, "ttl_seconds": 60})
        statuses.append(response.status_code)
    assert statuses[:30] == [201] * 30
    assert statuses[30] == 429
    events = [row["event"] for row in audit.stats()["recent_actions"]]
    assert "admin_rate_limited" in events
