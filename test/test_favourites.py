# -*- coding: utf-8 -*-
"""Favourites lifecycle and best-effort behaviour on a broken data dir."""

import json
import os
import stat

import pytest


STREAM_FAV = {"kind": "stream", "target": "https://cdn.example/ep1.mp3", "title": "Episode 1"}
PACK_FAV = {"kind": "pack", "target": "dreamscape-10", "title": "Dreamscape 10"}


def test_favourites_empty(web):
    payload = web.get("/api/favourites").get_json()
    assert payload == {"title": "Favourites", "items": []}


def test_add_and_list_stream_favourite(web):
    response = web.post("/api/favourites", json=STREAM_FAV)
    assert response.status_code == 200
    assert response.get_json() == {"saved": True}

    payload = web.get("/api/favourites").get_json()
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["label"] == "Episode 1"
    assert item["play"] == "https://cdn.example/ep1.mp3"
    assert item["fav_key"] == "stream:https://cdn.example/ep1.mp3"


def test_add_pack_favourite_opens_item(web):
    assert web.post("/api/favourites", json=PACK_FAV).get_json() == {"saved": True}
    item = web.get("/api/favourites").get_json()["items"][0]
    assert item["open"] == "/api/item/dreamscape-10"
    assert item["fav_key"] == "pack:dreamscape-10"
    assert item["sub"] == "Tape pack"


def test_add_is_idempotent(web):
    web.post("/api/favourites", json=STREAM_FAV)
    web.post("/api/favourites", json=STREAM_FAV)
    items = web.get("/api/favourites").get_json()["items"]
    assert len(items) == 1


def test_add_moves_existing_to_the_top(web):
    web.post("/api/favourites", json=PACK_FAV)
    web.post("/api/favourites", json=STREAM_FAV)
    items = web.get("/api/favourites").get_json()["items"]
    assert [item["label"] for item in items] == ["Episode 1", "Dreamscape 10"]


def test_delete_lifecycle(web):
    web.post("/api/favourites", json=STREAM_FAV)
    response = web.delete("/api/favourites/stream:https://cdn.example/ep1.mp3")
    assert response.status_code == 200
    assert response.get_json() == {"removed": True}
    assert web.get("/api/favourites").get_json()["items"] == []

    again = web.delete("/api/favourites/stream:https://cdn.example/ep1.mp3")
    assert again.status_code == 404


def test_delete_does_not_touch_other_entries(web):
    web.post("/api/favourites", json=STREAM_FAV)
    web.post("/api/favourites", json=PACK_FAV)
    web.delete("/api/favourites/pack:dreamscape-10")
    items = web.get("/api/favourites").get_json()["items"]
    assert [item["label"] for item in items] == ["Episode 1"]


@pytest.mark.parametrize("payload", [
    {},
    {"title": "x"},
    {"target": "https://x.example/a.mp3"},
    {"kind": "bogus", "title": "x", "target": "https://x.example/a.mp3"},
    {"kind": "stream", "title": "x", "target": "javascript:alert(1)"},
    {"kind": "stream", "title": "x", "target": "not a url"},
    {"kind": "stream", "title": "x", "target": "http://[::1/" },
    {"kind": "pack", "title": {"nested": 1}, "target": "x"},
    {"kind": "pack", "title": "x", "target": "  "},
])
def test_invalid_payloads_are_400(web, payload):
    response = web.post("/api/favourites", json=payload)
    assert response.status_code == 400
    body = response.get_json()
    assert body["saved"] is False
    assert isinstance(body["error"], str) and body["error"]
    assert web.get("/api/favourites").get_json()["items"] == []


def test_non_object_json_is_400(web):
    for body in ([], "hello", 42, None):
        response = web.post("/api/favourites", json=body)
        assert response.status_code == 400
        assert response.get_json()["saved"] is False


def test_missing_json_is_400(web):
    response = web.post("/api/favourites", data="not json", content_type="application/json")
    assert response.status_code == 400


def test_oversized_body_is_413_json(web):
    response = web.post("/api/favourites", data=b"x" * (70 * 1024),
                        content_type="application/json")
    assert response.status_code == 413
    assert response.get_json() == {"error": "request body too large"}


def test_corrupt_store_is_ignored(web, tmp_path, monkeypatch):
    fav_dir = tmp_path / "favs-broken"
    fav_dir.mkdir()
    (fav_dir / "favourites.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(fav_dir))
    response = web.get("/api/favourites")
    assert response.status_code == 200
    assert response.get_json()["items"] == []
    assert web.post("/api/favourites", json=STREAM_FAV).get_json() == {"saved": True}


def test_entries_without_target_are_skipped(web, tmp_path, monkeypatch):
    fav_dir = tmp_path / "favs-partial"
    fav_dir.mkdir()
    (fav_dir / "favourites.json").write_text(json.dumps([
        {"title": "no target"},
        {"kind": "stream", "title": "ok", "target": "https://x.example/a.mp3"},
        {"kind": "pack", "title": "no target either"},
        {"kind": "stream", "title": "bad target type", "target": 42},
        "not a dict",
    ]), encoding="utf-8")
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(fav_dir))
    payload = web.get("/api/favourites").get_json()
    assert [item["label"] for item in payload["items"]] == ["ok"]


def test_read_only_data_dir_still_works(web, tmp_path, monkeypatch):
    fav_dir = tmp_path / "favs-ro"
    fav_dir.mkdir()
    os.chmod(fav_dir, stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(fav_dir))
    try:
        response = web.post("/api/favourites", json=STREAM_FAV)
        assert response.status_code == 200
        assert response.get_json() == {"saved": False}
        listed = web.get("/api/favourites")
        assert listed.status_code == 200
        assert listed.get_json()["items"] == []
        assert web.delete("/api/favourites/stream:x").status_code == 404
    finally:
        os.chmod(fav_dir, stat.S_IRWXU)


def test_data_dir_path_is_a_file(web, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(blocker))
    assert web.get("/api/favourites").status_code == 200
    assert web.post("/api/favourites", json=STREAM_FAV).get_json() == {"saved": False}
    assert web.delete("/api/favourites/x").status_code == 404


def test_broken_provider_does_not_500(web, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("app.favourites.load", boom)
    assert web.get("/api/favourites").status_code == 200
    monkeypatch.setattr("app.favourites.add", boom)
    assert web.post("/api/favourites", json=STREAM_FAV).get_json() == {"saved": False}
    monkeypatch.setattr("app.favourites.remove", boom)
    assert web.delete("/api/favourites/x").status_code == 404


def test_legacy_referer_target_is_accepted(web):
    target = ("https://www.ravetapepacks.com/music/E/Bradley_M.mp3"
              "|Referer=https://www.ravetapepacks.com/&User-Agent=Mozilla/5.0")
    response = web.post("/api/favourites", json={
        "kind": "stream", "title": "Bradley M", "target": target})
    assert response.get_json() == {"saved": True}
    item = web.get("/api/favourites").get_json()["items"][0]
    assert item["play"] == target
    assert item["fav_key"] == "stream:" + target


def test_store_is_plain_json(web, tmp_path, monkeypatch):
    fav_dir = tmp_path / "favs-json"
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(fav_dir))
    web.post("/api/favourites", json=STREAM_FAV)
    stored = json.loads((fav_dir / "favourites.json").read_text(encoding="utf-8"))
    assert stored[0]["title"] == "Episode 1"
    assert stored[0]["kind"] == "stream"


# --------------------------------------------------------------------------- atomic save


def test_save_is_atomic_in_the_same_directory(tmp_path, monkeypatch):
    """One fsync'd temp file next to the store, then a single os.replace."""
    from core import favourites

    fav_dir = tmp_path / "favs-atomic"
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(fav_dir))
    replaces, fsyncs = [], []
    real_replace, real_fsync = os.replace, os.fsync

    def spy_replace(src, dst):
        replaces.append((src, dst))
        real_replace(src, dst)

    def spy_fsync(fd):
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(favourites.os, "replace", spy_replace)
    monkeypatch.setattr(favourites.os, "fsync", spy_fsync)

    assert favourites.save([PACK_FAV, STREAM_FAV]) is True
    assert len(replaces) == 1
    temporary, destination = replaces[0]
    assert os.path.dirname(temporary) == os.path.dirname(destination) == str(fav_dir)
    assert os.path.basename(temporary).startswith(".tmp-")
    assert destination == str(fav_dir / "favourites.json")
    assert fsyncs, "the temp file must be fsync'd before the replace"
    assert not list(fav_dir.glob(".tmp-*")), "temporary file left behind"
    assert [entry["title"] for entry in favourites.load()] == ["Dreamscape 10", "Episode 1"]


def test_failed_replace_keeps_the_previous_store(tmp_path, monkeypatch):
    """A crash/full disk during save must not truncate the old favourites."""
    from core import favourites

    fav_dir = tmp_path / "favs-failed"
    monkeypatch.setenv("RAVEPLAYER_FAV_DIR", str(fav_dir))
    assert favourites.save([PACK_FAV]) is True

    def explode(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(favourites.os, "replace", explode)
    assert favourites.save([STREAM_FAV]) is False
    assert [entry["title"] for entry in favourites.load()] == ["Dreamscape 10"]
    assert not list(fav_dir.glob(".tmp-*")), "failed save left a temporary file"
