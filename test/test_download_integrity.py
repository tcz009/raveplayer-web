# -*- coding: utf-8 -*-
"""Download integrity (QA 1.6.62): truncated saves, shared cancel, RTP flag.

Offline only: the network is replaced at ``app._open_stream`` and the shared
cancel-store tests use short-lived subprocesses pointed at one temp data dir.
"""

import http.client
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

import app as app_module

HARDSCORE_URL = "https://hardscore.com/mixes/test-mix.mp3"
ARCHIVE_URL = "https://archive.org/download/boot-pack/boot-track.mp3"
RTP_URL = "https://www.ravetapepacks.com/music/Pack/Tape.mp3"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeResponse(object):
    """An upstream body with optional Content-Length and injected failures."""

    def __init__(self, chunks, length="auto", content_type="audio/mpeg", status=200):
        self.status = status
        self._chunks = list(chunks)
        self._content_type = content_type
        if length == "auto":
            self._length = sum(len(chunk) for chunk in self._chunks
                               if isinstance(chunk, bytes))
        else:
            self._length = length

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return self._content_type
        if lowered == "content-length":
            return None if self._length is None else str(self._length)
        return None

    def read(self, size=-1):
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        pass


class _FakeConnection(object):
    def close(self):
        pass


def _patch_stream(monkeypatch, response, url=HARDSCORE_URL):
    monkeypatch.setattr(
        app_module, "_open_stream",
        lambda target, headers, deadline: (_FakeConnection(), response, url))


def _download(url=HARDSCORE_URL, destination="out.mp3", **kwargs):
    return list(app_module._download_chunks(
        url, destination, 10 * 1024 * 1024, 30, **kwargs))


def _post_download(web, **overrides):
    payload = {"url": HARDSCORE_URL, "title": "Integrity", "source": "hardscore",
               "client_id": "integ" + uuid.uuid4().hex[:8]}
    payload.update(overrides)
    return web.post("/api/downloads", data=json.dumps(payload),
                    content_type="application/json",
                    headers={"Accept": "application/x-ndjson"})


def _saved_files(base):
    folder = os.path.join(str(base), "downloads")
    if not os.path.isdir(folder):
        return []
    return sorted(name for name in os.listdir(folder)
                  if name.endswith((".mp3", ".m4a")))


# --------------------------------------------------------------------------- QA-01


def test_short_body_with_content_length_is_rejected_and_partial_removed(tmp_path,
                                                                       monkeypatch):
    response = _FakeResponse([b"a" * 400, b"b" * 400], length=1000)
    _patch_stream(monkeypatch, response)
    destination = tmp_path / "short.mp3"
    with pytest.raises(app_module.DownloadError) as exc:
        _download(destination=str(destination))
    assert exc.value.code == 502
    assert "incomplete" in exc.value.message
    assert not destination.exists()


def test_overlong_body_with_content_length_is_rejected(tmp_path, monkeypatch):
    response = _FakeResponse([b"a" * 600, b"b" * 600], length=1000)
    _patch_stream(monkeypatch, response)
    destination = tmp_path / "long.mp3"
    with pytest.raises(app_module.DownloadError) as exc:
        _download(destination=str(destination))
    assert exc.value.code == 502
    assert "more data" in exc.value.message or "incomplete" in exc.value.message
    assert not destination.exists()


def test_mid_stream_incomplete_read_is_not_saved(tmp_path, monkeypatch):
    response = _FakeResponse([b"a" * 500,
                              http.client.IncompleteRead(b"", 500)], length=1000)
    _patch_stream(monkeypatch, response)
    destination = tmp_path / "eof.mp3"
    with pytest.raises(app_module.DownloadError) as exc:
        _download(destination=str(destination))
    assert exc.value.code == 502
    assert "incomplete" in exc.value.message
    assert not destination.exists()


def test_no_content_length_still_saves_normally(tmp_path, monkeypatch):
    response = _FakeResponse([b"a" * 500, b"b" * 500], length=None)
    _patch_stream(monkeypatch, response)
    destination = tmp_path / "chunked.mp3"
    events = _download(destination=str(destination))
    assert events[0] == {"started": True, "total": None}
    assert events[-1] == {"done": True, "size": 1000}
    assert destination.read_bytes() == b"a" * 500 + b"b" * 500


def test_foreground_stream_reports_error_and_leaves_no_item(web, tmp_path, monkeypatch):
    _patch_stream(monkeypatch, _FakeResponse([b"a" * 400], length=1000))
    response = _post_download(web)
    lines = [json.loads(line) for line in
             response.get_data(as_text=True).splitlines() if line.strip()]
    assert lines[-1]["state"] == "error"
    assert lines[-1]["code"] == 502
    assert "incomplete" in lines[-1]["error"]
    assert web.get("/api/downloads").get_json()["items"] == []
    assert _saved_files(tmp_path) == []


def test_foreground_plain_response_is_502(web, tmp_path, monkeypatch):
    _patch_stream(monkeypatch, _FakeResponse([b"a" * 400], length=1000))
    payload = {"url": HARDSCORE_URL, "title": "Truncated", "source": "hardscore"}
    response = web.post("/api/downloads", data=json.dumps(payload),
                        content_type="application/json")
    assert response.status_code == 502
    assert "incomplete" in response.get_json()["error"]
    assert _saved_files(tmp_path) == []


def _wait_job(web, job_id, state, timeout=8.0):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = web.get("/api/downloads/jobs/%s" % job_id).get_json()["job"]
        if job["state"] == state:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not become %s: %r" % (state, job))


def test_background_job_marks_truncation_failed_not_completed(web, tmp_path,
                                                              monkeypatch):
    _patch_stream(monkeypatch, _FakeResponse([b"a" * 400], length=1000),
                  url=ARCHIVE_URL)
    response = web.post("/api/downloads/jobs", json={
        "url": ARCHIVE_URL, "title": "Truncated job", "source": "archive"})
    assert response.status_code == 202
    job = _wait_job(web, response.get_json()["id"], "failed")
    assert "incomplete" in job["error"]
    assert job["download_id"] == ""
    assert web.get("/api/downloads").get_json()["items"] == []
    assert _saved_files(tmp_path) == []


def test_saved_copy_with_wrong_size_is_not_served(web, tmp_path, monkeypatch):
    _patch_stream(monkeypatch, _FakeResponse([b"x" * 1000], length=1000))
    payload = {"url": HARDSCORE_URL, "title": "Good copy", "source": "hardscore"}
    item = web.post("/api/downloads", data=json.dumps(payload),
                    content_type="application/json").get_json()["item"]
    folder = os.path.join(str(tmp_path), "downloads")
    filename = next(name for name in os.listdir(folder) if name.endswith(".mp3"))
    with open(os.path.join(folder, filename), "r+b") as handle:
        handle.truncate(100)
    for suffix in ("stream", "file"):
        response = web.get("/api/downloads/%s/%s" % (item["download_key"], suffix))
        assert response.status_code == 409, suffix
        assert response.get_json()["code"] == "download_incomplete"


# --------------------------------------------------------------------------- QA-02


def _worker_env(base):
    env = dict(os.environ)
    env.update({
        "RAVEWEB_DATA": str(base),
        "RAVEWEB_PASSWORD": "test-password",
        "RAVEWEB_FRIENDS_PASSWORD": "friends-password",
        "SECRET_KEY": "test-secret-key",
        "RAVEWEB_ENV": "test",
        "PYTHONPATH": REPO,
    })
    return env


_SESSION_CODE = '''
import time
from core import audit
sid = audit.create_session("owner", "admin", "127.0.0.1", "pytest", 3600)
with client.session_transaction() as sess:
    sess["auth"] = True
    sess["identity"] = "owner"
    sess["user"] = "owner"
    sess["role"] = "admin"
    sess["sid"] = sid
    sess["login_at"] = time.time()
    sess["remember"] = False
    sess["csrf"] = "test-csrf"
'''


def _spawn_worker(code, base):
    return subprocess.Popen(
        [sys.executable, "-c", code], cwd=REPO, env=_worker_env(base),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cancel_is_shared_between_two_processes_and_removes_partial(tmp_path):
    """Worker A streams; a separately spawned worker B cancels it.

    Both are independent Python processes sharing only ``RAVEWEB_DATA`` (which
    holds auth.sqlite3 and the downloads folder), exactly like two gunicorn
    workers.  The file must never be registered, and A must report cancelled.
    """
    client_id = "xproc" + uuid.uuid4().hex[:8]
    streamed = tmp_path / "streamed.marker"
    result = tmp_path / "worker-a.ndjson"
    total = 200 * 65536
    worker_a = '''
import json, os, time
import app as app_module
from core import audit
base = %r
audit.configure(os.path.join(base, "auth.sqlite3"))
app_module.DATA_DIR = base
app_module.app.config.update(TESTING=True)
client = app_module.app.test_client()
%s
TOTAL = %d
MARKER = %r
class Resp(object):
    status = 200
    def __init__(self):
        self.sent = 0
    def getheader(self, name):
        name = (name or "").lower()
        if name == "content-type":
            return "audio/mpeg"
        if name == "content-length":
            return str(TOTAL)
        return None
    def read(self, size=-1):
        if self.sent == 0:
            with open(MARKER, "w") as handle:
                handle.write("started")
        if self.sent >= TOTAL:
            return b""
        time.sleep(0.1)
        want = min(size if size and size > 0 else 65536, 65536, TOTAL - self.sent)
        self.sent += want
        return b"x" * want
    def close(self):
        pass
class Conn(object):
    def close(self):
        pass
def fake_open_stream(url, headers, deadline):
    return Conn(), Resp(), url
app_module._open_stream = fake_open_stream
response = client.post(
    "/api/downloads",
    data=json.dumps({"url": %r, "title": "Cross worker",
                     "source": "hardscore", "client_id": %r}),
    content_type="application/json",
    headers={"Accept": "application/x-ndjson"})
with open(%r, "w") as handle:
    handle.write(response.get_data(as_text=True))
''' % (str(tmp_path), _SESSION_CODE, total, str(streamed), HARDSCORE_URL,
       client_id, str(result))
    worker_b = '''
import json
import os
import app as app_module
from core import audit
base = %r
audit.configure(os.path.join(base, "auth.sqlite3"))
app_module.DATA_DIR = base
app_module.app.config.update(TESTING=True)
client = app_module.app.test_client()
%s
response = client.post("/api/downloads/cancel/%s")
print(json.dumps({"status": response.status_code,
                  "body": response.get_json()}))
''' % (str(tmp_path), _SESSION_CODE, client_id)

    a = _spawn_worker(worker_a, tmp_path)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not streamed.exists():
            if a.poll() is not None:
                break
            time.sleep(0.02)
        assert streamed.exists(), a.communicate(timeout=5)[1]

        b = subprocess.run([sys.executable, "-c", worker_b], cwd=REPO,
                           env=_worker_env(tmp_path), capture_output=True,
                           text=True, timeout=30)
        assert b.returncode == 0, b.stderr
        cancelled = json.loads(b.stdout.strip().splitlines()[-1])
        assert cancelled["status"] == 200, cancelled
        assert cancelled["body"]["cancelled"] is True

        out, err = a.communicate(timeout=45)
        assert a.returncode == 0, err
        lines = [json.loads(line) for line in
                 (result.read_text()).splitlines() if line.strip()]
        assert lines[-1]["state"] == "cancelled", lines
        assert not any(line.get("state") == "saved" for line in lines)
        assert _saved_files(tmp_path) == []
        assert not any(name.startswith(".tmp-")
                       for name in os.listdir(os.path.join(str(tmp_path), "downloads")))
    finally:
        if a.poll() is None:
            a.kill()
            a.wait(timeout=5)


def test_cancel_endpoint_rejects_ids_that_never_existed(web):
    response = web.post("/api/downloads/cancel/neverexisted1")
    assert response.status_code == 404
    assert response.get_json()["error"] == "unknown download"


# --------------------------------------------------------------------------- QA-05 / P2.3


def test_ravetapepacks_downloads_are_hidden_but_playback_stays(web, monkeypatch):
    monkeypatch.setattr(app_module.legacy, "post", lambda post_id: {
        "key": post_id, "name": "Dreamscape 7",
        "tracks": [{"name": "Charlie Hall", "url": RTP_URL}],
    } if post_id == "899" else None)
    item = web.get("/api/legacy/ravetapepacks/post/899").get_json()["items"][0]
    assert item["play"]
    assert "download" not in item

    rejected = web.post("/api/downloads", data=json.dumps({
        "url": RTP_URL, "title": "RTP", "source": "ravetapepacks"}),
        content_type="application/json")
    assert rejected.status_code == 400
    assert "temporarily disabled" in rejected.get_json()["error"]

    monkeypatch.setattr(app_module, "RTP_DOWNLOADS_ENABLED", True)
    item = web.get("/api/legacy/ravetapepacks/post/899").get_json()["items"][0]
    assert item["download"] == {"source": "ravetapepacks"}
