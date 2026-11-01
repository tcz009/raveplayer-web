# -*- coding: utf-8 -*-
"""Per-identity favourites/downloads scopes (1.6.26)."""

import json
import os
import re
import threading

import app as app_module
from core import downloads, favourites

OWNER_URL = "https://hardscore.com/mixes/owner-mix.mp3"
FRIENDS_URL = "https://hardscore.com/mixes/friends-mix.mp3"


class _FakeResponse(object):
    def __init__(self, body=b"ID3" + b"x" * 1000):
        self.status = 200
        self._body = body
        self._offset = 0

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return "audio/mpeg"
        if lowered == "content-length":
            return str(len(self._body))
        return None

    def read(self, size=-1):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeConnection(object):
    def close(self):
        pass


def _patch_stream(monkeypatch):
    def fake(url, headers, deadline):
        return _FakeConnection(), _FakeResponse(), url
    monkeypatch.setattr(app_module, "_open_stream", fake)


def _client():
    return app_module.app.test_client()


def _login(client, identity="friends", password="friends-password"):
    page = client.get("/login")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    return client.post("/login", data={
        "csrf": token, "username": identity, "password": password, "next": "/"})


def _add_fav(client, title, target, kind="stream"):
    return client.post("/api/favourites", json={
        "kind": kind, "title": title, "target": target})


def test_favourites_are_isolated_and_cross_user_delete_is_404(web):
    friend = _client()
    assert _login(friend).status_code == 302
    assert web.get("/api/favourites").get_json()["items"] == []
    assert friend.get("/api/favourites").get_json()["items"] == []

    assert _add_fav(web, "Owner fav", "https://cdn.example/owner.mp3").status_code == 200
    assert _add_fav(friend, "Friend fav", "https://cdn.example/friend.mp3").status_code == 200
    assert [i["label"] for i in web.get("/api/favourites").get_json()["items"]] == ["Owner fav"]
    assert [i["label"] for i in friend.get("/api/favourites").get_json()["items"]] == ["Friend fav"]
    assert web.delete("/api/favourites/stream:https://cdn.example/friend.mp3").status_code == 404
    assert friend.delete("/api/favourites/stream:https://cdn.example/owner.mp3").status_code == 404
    assert friend.delete("/api/favourites/stream:https://cdn.example/friend.mp3").status_code == 200
    assert friend.get("/api/favourites").get_json()["items"] == []
    assert web.get("/api/favourites").get_json()["items"][0]["label"] == "Owner fav"


def test_owner_all_flag_lists_both_favourite_scopes_but_friends_cannot(web):
    friend = _client()
    assert _login(friend).status_code == 302
    _add_fav(web, "Owner fav", "https://cdn.example/owner.mp3")
    _add_fav(friend, "Friend fav", "https://cdn.example/friend.mp3")
    everything = web.get("/api/favourites?all=1").get_json()["items"]
    assert {item["label"] for item in everything} == {"Owner fav", "Friend fav"}
    assert {item.get("identity") for item in everything} == {"owner", "friends"}
    friend_view = friend.get("/api/favourites?all=1").get_json()["items"]
    assert [item["label"] for item in friend_view] == ["Friend fav"]


def test_favourites_migration_adopts_legacy_file_for_owner(web, monkeypatch):
    # The pre-1.6.26 global file (no identity argument) is the owner's store.
    assert favourites.save([{"kind": "stream", "title": "Legacy fav",
                             "target": "https://cdn.example/legacy.mp3"}]) is True
    owner_items = web.get("/api/favourites").get_json()["items"]
    assert [item["label"] for item in owner_items] == ["Legacy fav"]
    friend = _client()
    assert _login(friend).status_code == 302
    assert friend.get("/api/favourites").get_json()["items"] == []
    first = favourites.migrate_legacy()
    second = favourites.migrate_legacy()
    assert first == second
    assert first["owner"] == 1 and first["friends"] == 0


def test_downloads_are_isolated_and_owner_all_flag(web, monkeypatch, tmp_path):
    _patch_stream(monkeypatch)
    friend = _client()
    assert _login(friend).status_code == 302
    assert web.get("/api/downloads").get_json()["items"] == []
    assert friend.get("/api/downloads").get_json()["items"] == []

    owner = web.post("/api/downloads", data=json.dumps({
        "url": OWNER_URL, "title": "Owner mix", "source": "hardscore"}),
        content_type="application/json")
    mates = friend.post("/api/downloads", data=json.dumps({
        "url": FRIENDS_URL, "title": "Friends mix", "source": "hardscore"}),
        content_type="application/json")
    assert owner.status_code == 200 and mates.status_code == 200
    owner_key = owner.get_json()["item"]["download_key"]
    friend_key = mates.get_json()["item"]["download_key"]
    assert owner_key != friend_key

    assert [i["label"] for i in web.get("/api/downloads").get_json()["items"]] == ["Owner mix"]
    assert [i["label"] for i in friend.get("/api/downloads").get_json()["items"]] == ["Friends mix"]
    # Owner default view stays own-only; all=1 includes the friends entry.
    own = web.get("/api/downloads").get_json()["items"]
    assert len(own) == 1 and own[0]["label"] == "Owner mix"
    everything = web.get("/api/downloads?all=1").get_json()["items"]
    assert {item["label"] for item in everything} == {"Owner mix", "Friends mix"}
    assert {item["identity"] for item in everything} == {"owner", "friends"}
    assert [i["label"] for i in friend.get("/api/downloads?all=1").get_json()["items"]] == ["Friends mix"]

    # Cross-user stream/file/delete is a 404, never 403/leak.
    assert friend.get("/api/downloads/%s/stream" % owner_key).status_code == 404
    assert friend.get("/api/downloads/%s/file" % owner_key).status_code == 404
    assert friend.delete("/api/downloads/%s" % owner_key).status_code == 404
    assert web.get("/api/downloads/%s/stream" % friend_key).status_code == 404
    assert web.delete("/api/downloads/%s" % friend_key).status_code == 404
    assert web.get("/api/downloads/%s/stream" % owner_key).status_code == 200
    assert friend.get("/api/downloads/%s/stream" % friend_key).status_code == 200

    # Files/indexes live in separate scopes; deleting one leaves the other.
    assert os.path.isdir(os.path.join(tmp_path, "downloads", "friends"))
    assert friend.delete("/api/downloads/%s" % friend_key).status_code == 200
    assert friend.get("/api/downloads").get_json()["items"] == []
    assert web.get("/api/downloads").get_json()["items"][0]["label"] == "Owner mix"


def test_downloads_migration_adopts_legacy_library(web, monkeypatch, tmp_path):
    entry = downloads.entry_for(str(tmp_path), "Legacy download", OWNER_URL, "hardscore")
    temp = downloads.temp_path(str(tmp_path), entry)
    with open(temp, "wb") as handle:
        handle.write(b"ID3" + b"z" * 1000)
    entry["size"] = len(b"ID3" + b"z" * 1000)
    downloads.register(str(tmp_path), entry, temp, 10 ** 9, 200)
    assert [i["label"] for i in web.get("/api/downloads").get_json()["items"]] == ["Legacy download"]
    friend = _client()
    assert _login(friend).status_code == 302
    assert friend.get("/api/downloads").get_json()["items"] == []
    assert downloads.migrate_legacy(str(tmp_path)) == downloads.migrate_legacy(str(tmp_path))
    assert downloads.migrate_legacy(str(tmp_path))["owner"] == 1


def test_friends_background_job_completes_into_their_library_only(web, monkeypatch):
    release = threading.Event()

    def fake_chunks(url, destination, max_bytes, seconds, should_cancel=None):
        yield {"started": True, "total": 4}
        release.wait(3)
        with open(destination, "wb") as handle:
            handle.write(b"ID3x")
        yield {"done": True, "size": 4}

    import app as app_module
    monkeypatch.setattr(app_module, "_download_chunks", fake_chunks)
    friend = _client()
    assert _login(friend).status_code == 302
    response = friend.post("/api/downloads/jobs", json={
        "url": FRIENDS_URL, "title": "Friends background", "source": "hardscore"})
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    release.set()
    deadline = __import__("time").time() + 5
    while __import__("time").time() < deadline:
        job = friend.get("/api/downloads/jobs/%s" % job_id).get_json()["job"]
        if job["state"] == "completed":
            break
        __import__("time").sleep(0.02)
    assert job["state"] == "completed"
    assert [i["label"] for i in friend.get("/api/downloads").get_json()["items"]] == ["Friends background"]
    assert web.get("/api/downloads").get_json()["items"] == []
