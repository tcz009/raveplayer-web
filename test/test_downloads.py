# -*- coding: utf-8 -*-
"""Downloads/offline phase 1 (web 1.6.16): eligibility, storage guards,
Range serving, safe filenames, and the offline library API.

The proxy download reuses app._open_stream; every test replaces that with a
tiny local response so the suite never touches the network.
"""

import json
import os

import pytest

ARCHIVE_URL = "https://archive.org/download/boot-pack/boot-track.mp3"
HARDSCORE_URL = "https://hardscore.com/mixes/test-mix.mp3"
MOXSER_URL = "https://enceladus.feralhosting.com/moxser/2026/1/test.mp3"


class _FakeResponse(object):
    def __init__(self, body, content_type="audio/mpeg", length=None, status=200):
        self.status = status
        self._body = body
        self._offset = 0
        self._content_type = content_type
        self._length = len(body) if length is None else length

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return self._content_type
        if lowered == "content-length":
            return None if self._length is None else str(self._length)
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


def _patch_stream(monkeypatch, body=b"ID3" + b"a" * 1000, **kwargs):
    import app as app_module

    def fake_open_stream(url, headers, deadline):
        return _FakeConnection(), _FakeResponse(body, **kwargs), url

    monkeypatch.setattr(app_module, "_open_stream", fake_open_stream)


def _post(web, payload):
    return web.post("/api/downloads", data=json.dumps(payload),
                    content_type="application/json")


def _files(tmp_path):
    folder = tmp_path / "downloads"
    if not folder.exists():
        return []
    return sorted(p.name for p in folder.glob("*")
                  if not p.name.startswith(".") and p.name != "index.json")


# --------------------------------------------------------------------------- rules


def test_eligibility_rules_cover_the_four_sources_and_block_platform_pages():
    import app as app_module

    allowed = app_module._download_allowed
    assert allowed("archive", ARCHIVE_URL)[0]
    assert allowed("moxser", MOXSER_URL)[0]
    assert allowed("hardscore", HARDSCORE_URL)[0]
    pirate = [entry for entry in app_module.pirate.entries()
              if entry["stream_url"].startswith("https://")]
    assert pirate, "seed data should contain a direct pirate file"
    assert allowed("pirate", pirate[0]["stream_url"])[0]
    assert not allowed("pirate", "https://example.com/x.mp3")[0]
    for source, url in (
        ("moxser", "https://www.mixcloud.com/djez/x/"),
        ("moxser", "https://www.soundcloud.com/x/y"),
        ("moxser", "https://www.youtube.com/watch?v=x"),
        ("moxser", "https://www.mixcloud.com/widget/iframe/?feed=x"),
        ("moxser", "javascript:alert(1)"),
        ("archive", "https://hardscore.com/mixes/x.mp3"),
        ("archive", "https://archive.org/details/x"),
        ("moxser", "https://enceladus.feralhosting.com/moxser/x"),
        ("podcast", ARCHIVE_URL),
    ):
        ok, _message = allowed(source, url)
        assert not ok, (source, url)


def test_safe_filename_and_stable_id(tmp_path):
    from core import downloads

    entry = downloads.entry_for(str(tmp_path), "../../Dreamscape 7: Live / 26.11.93",
                                HARDSCORE_URL, "hardscore")
    assert entry["id"] and len(entry["id"]) == 16
    assert entry["filename"].endswith(".mp3")
    assert "/" not in entry["filename"] and ".." not in entry["filename"]
    assert entry["filename"].startswith("Dreamscape-7-Live-26.11.93-")
    same = downloads.entry_for(str(tmp_path), "Another title", HARDSCORE_URL, "hardscore")
    assert same["id"] == entry["id"]  # id is the URL hash, not the title
    m4a = downloads.entry_for(str(tmp_path), "x", "https://hardscore.com/a.m4a", "hardscore")
    assert m4a["filename"].endswith(".m4a")


# --------------------------------------------------------------------------- API flow


def test_download_saves_lists_plays_offline_and_deletes(web, tmp_path, monkeypatch):
    _patch_stream(monkeypatch)
    response = _post(web, {"url": HARDSCORE_URL, "title": "Test Mix", "source": "hardscore"})
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["saved"] is True
    identifier = payload["item"]["download_key"]
    assert payload["item"]["play"].endswith("/stream")
    assert _files(tmp_path) and any(name.endswith(".mp3") for name in _files(tmp_path))

    listing = web.get("/api/downloads").get_json()
    assert listing["title"] == "Downloads"
    assert len(listing["items"]) == 1
    item = listing["items"][0]
    assert item["label"] == "Test Mix"
    assert item["download_key"] == identifier
    assert item["sub"].startswith("1003 B")  # exact byte size
    assert listing["storage"]["used"] == 1003
    assert listing["storage"]["limit"] > 0

    # Re-downloading the same URL does not create a second copy.
    again = _post(web, {"url": HARDSCORE_URL, "title": "Test Mix", "source": "hardscore"})
    assert again.get_json()["already"] is True
    assert len(web.get("/api/downloads").get_json()["items"]) == 1

    # Offline playback: same-origin local copy with Range support.
    ranged = web.get(item["play"], headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.data == b"ID3a"
    assert ranged.headers["Content-Range"].startswith("bytes 0-3/1003")
    assert ranged.headers["Content-Type"].startswith("audio/")
    assert ranged.headers["Cross-Origin-Resource-Policy"] == "same-origin"

    # The "save to this device" endpoint is an attachment with a safe filename.
    attachment = web.get(item["file"])
    assert attachment.status_code == 200
    disposition = attachment.headers["Content-Disposition"]
    assert "attachment" in disposition and ".mp3" in disposition
    assert ".." not in disposition and "/" not in disposition.replace("attachment", "")
    ranged_file = web.get(item["file"], headers={"Range": "bytes=100-199"})
    assert ranged_file.status_code == 206
    assert len(ranged_file.data) == 100

    removed = web.delete("/api/downloads/%s" % identifier).get_json()
    assert removed["removed"] is True
    assert web.get("/api/downloads").get_json()["items"] == []
    assert not any(name.endswith(".mp3") for name in _files(tmp_path))
    assert web.get("/api/downloads/%s/stream" % identifier).status_code == 404
    assert web.delete("/api/downloads/nope").get_json()["removed"] is False


def test_download_rejects_ineligible_requests(web, tmp_path, monkeypatch):
    _patch_stream(monkeypatch)
    cases = [
        {"url": "https://www.mixcloud.com/djez/x/", "source": "moxser", "title": "x"},
        {"url": "javascript:alert(1)", "source": "moxser", "title": "x"},
        {"url": "https://hardscore.com/mixes/x.mp3", "source": "archive", "title": "x"},
        {"url": "https://hardscore.com/mixes/x", "source": "hardscore", "title": "x"},
        {"url": ARCHIVE_URL, "source": "podcast", "title": "x"},
        {"url": "", "source": "archive", "title": "x"},
    ]
    for payload in cases:
        response = _post(web, payload)
        assert response.status_code == 400, (payload, response.status_code)
    assert _files(tmp_path) == []
    assert web.post("/api/downloads", data="not json",
                    content_type="application/json").status_code == 400


def test_no_automatic_downloads(web, tmp_path):
    listing = web.get("/api/downloads").get_json()
    assert listing["items"] == []
    assert _files(tmp_path) == []


def test_per_file_size_guard(web, tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "DOWNLOAD_MAX_BYTES", 100)
    _patch_stream(monkeypatch, body=b"x" * 1000, length=1000)
    response = _post(web, {"url": HARDSCORE_URL, "title": "big", "source": "hardscore"})
    assert response.status_code == 413
    assert web.get("/api/downloads").get_json()["items"] == []
    assert _files(tmp_path) == []


def test_library_size_guard(web, tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "DOWNLOAD_MAX_TOTAL", 1500)
    _patch_stream(monkeypatch, body=b"x" * 1000, length=1000)
    assert _post(web, {"url": HARDSCORE_URL, "title": "one",
                       "source": "hardscore"}).status_code == 200
    other = "https://hardscore.com/mixes/second.mp3"
    response = _post(web, {"url": other, "title": "two", "source": "hardscore"})
    assert response.status_code == 413
    assert len(web.get("/api/downloads").get_json()["items"]) == 1


def test_download_markers_only_on_the_four_sources(web, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.moxser, "mixes", lambda limit=48: [{
        "slug": "alpha-show", "title": "Alpha Show", "date": "2026-10-26", "flyer": "",
    }])
    monkeypatch.setattr(app_module.hardscore, "mixes", lambda limit=30: [{
        "title": "Hardscore Mix", "media": HARDSCORE_URL, "link": "", "date": "",
    }])
    monkeypatch.setattr(app_module.hardscore, "events", lambda limit=10: [])
    monkeypatch.setattr(app_module.archive, "audio_files", lambda identifier: [{
        "title": "Archive Track", "url": ARCHIVE_URL, "length": 100, "name": "a.mp3",
    }])

    assert web.get("/api/item/boot-pack").get_json()["items"][0]["download"]["source"] == "archive"
    assert web.get("/api/moxser").get_json()["items"][0]["download"]["source"] == "moxser"
    assert web.get("/api/hardscore").get_json()["items"][0]["download"]["source"] == "hardscore"
    risk = web.get("/api/pirate/risk-fm-107-1").get_json()["items"]
    playable = next(item for item in risk if item.get("play"))
    assert playable["download"]["source"] == "pirate"
    # Sources that are playable but not downloadable phase 1.
    assert "download" not in web.get("/api/legacy").get_json()["items"][0]
