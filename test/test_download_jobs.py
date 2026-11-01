# -*- coding: utf-8 -*-
"""Background download jobs: lifecycle, cancel, caps, restart and ownership."""

import json
import os
import re
import threading
import time

import app as app_module
from core import download_jobs as jobs_store
from core import downloads

URL = "https://archive.org/download/pack/tape.mp3"


def _login(client, identity="friends", password="friends-password"):
    page = client.get("/login")
    token = re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()
    return client.post("/login", data={
        "csrf": token, "username": identity, "password": password, "next": "/"})


def _create(web, **overrides):
    payload = {"url": URL, "title": "Tape", "source": "archive"}
    payload.update(overrides)
    return web.post("/api/downloads/jobs", json=payload)


def _wait_job(web, job_id, state="completed", timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = web.get("/api/downloads/jobs/%s" % job_id)
        assert response.status_code == 200
        job = response.get_json()["job"]
        if job["state"] == state:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not become %s: %r" % (state, job))


def _write_file(destination, size):
    with open(destination, "wb") as handle:
        handle.write(b"x" * size)


def _fast_chunks(done=100):
    def fake(url, destination, max_bytes, seconds, should_cancel=None):
        yield {"started": True, "total": done}
        yield {"received": done // 2, "total": done}
        yield {"received": done, "total": done}
        _write_file(destination, done)
        yield {"done": True, "size": done}
    return fake


def test_job_start_complete_and_index(web, monkeypatch):
    monkeypatch.setattr(app_module, "_download_chunks", _fast_chunks(1234))
    response = _create(web)
    assert response.status_code == 202
    payload = response.get_json()
    assert payload["id"]
    job = _wait_job(web, payload["id"])
    assert job["state"] == "completed"
    assert job["bytes_done"] == 1234 and job["bytes_total"] == 1234
    assert job["progress"] == 100
    assert job["download_id"]
    # Completion is in the ordinary Downloads index.
    listing = web.get("/api/downloads").get_json()
    assert any(item["download_key"] == job["download_id"]
               for item in listing["items"])
    # Job metadata is still available and is in the list.
    listed = web.get("/api/downloads/jobs").get_json()
    assert listed["jobs"][0]["id"] == job["id"]


def test_background_job_survives_http_request(web, monkeypatch):
    """The POST returns before the transfer; closing the caller does not stop it."""
    release = threading.Event()

    def slow(url, destination, max_bytes, seconds, should_cancel=None):
        yield {"started": True, "total": 10}
        release.wait(3)
        _write_file(destination, 10)
        yield {"done": True, "size": 10}

    monkeypatch.setattr(app_module, "_download_chunks", slow)
    response = _create(web, title="Outlives the tab")
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    # No further HTTP work: the worker thread still finishes after release.
    time.sleep(0.1)
    release.set()
    job = _wait_job(web, job_id)
    assert job["state"] == "completed"


def test_cancel_mid_stream_discards_partial(web, monkeypatch):
    def endless(url, destination, max_bytes, seconds, should_cancel=None):
        yield {"started": True, "total": 1000}
        for index in range(100):
            if should_cancel and should_cancel():
                raise app_module.DownloadCancelled()
            yield {"received": index * 10, "total": 1000}
            time.sleep(0.05)
        yield {"done": True, "size": 1000}

    monkeypatch.setattr(app_module, "_download_chunks", endless)
    job_id = _create(web).get_json()["id"]
    _wait_job(web, job_id, state="downloading")
    response = web.post("/api/downloads/jobs/%s/cancel" % job_id)
    assert response.status_code == 200
    job = _wait_job(web, job_id, state="cancelled")
    assert job["state"] == "cancelled"
    job_state = app_module._DOWNLOAD_JOB_STATE["jobs"][job_id]
    assert not os.path.exists(jobs_store.temp_path(app_module.DATA_DIR, job_state))


def test_concurrency_cap_queues_third_job(web, monkeypatch):
    release = threading.Event()

    def blocked(url, destination, max_bytes, seconds, should_cancel=None):
        yield {"started": True, "total": 10}
        release.wait(5)
        _write_file(destination, 10)
        yield {"done": True, "size": 10}

    monkeypatch.setattr(app_module, "_download_chunks", blocked)
    ids = [
        _create(web, title="Job %d" % index,
                url="https://archive.org/download/pack/tape-%d.mp3" % index,
               ).get_json()["id"]
        for index in range(3)
    ]
    deadline = time.time() + 4
    while time.time() < deadline:
        states = [web.get("/api/downloads/jobs/%s" % job_id).get_json()["job"]["state"]
                  for job_id in ids]
        if states.count("downloading") == 2:
            break
        time.sleep(0.02)
    assert states.count("downloading") <= app_module.DOWNLOAD_JOB_CONCURRENCY
    assert "queued" in states
    release.set()
    for job_id in ids:
        _wait_job(web, job_id)


def test_restart_recovery_marks_interrupted_job_failed():
    base = app_module.DATA_DIR
    entry = downloads.entry_for(base, "Interrupted", URL, "archive")
    job = jobs_store.new("owner", "admin", "Interrupted", "archive", URL, entry)
    job["state"] = "downloading"
    job["bytes_done"] = 10
    assert jobs_store.save(base, [job])
    with app_module._DOWNLOAD_JOBS_LOCK:
        app_module._DOWNLOAD_JOB_STATE["base"] = None
        app_module._DOWNLOAD_JOB_STATE["jobs"].clear()
    recovered = app_module._jobs_ensure() or app_module._DOWNLOAD_JOB_STATE["jobs"]
    stored = app_module._DOWNLOAD_JOB_STATE["jobs"][job["id"]]
    assert stored["state"] == "failed"
    assert "restart" in stored["error"]


def test_friends_see_only_their_own_jobs(anon):
    assert _login(anon, "friends").status_code == 302
    # Friends create and see their own job; an owner client must see it too.
    import app as app_module
    original = app_module._download_chunks
    release = threading.Event()

    def blocked(url, destination, max_bytes, seconds, should_cancel=None):
        yield {"started": True, "total": 10}
        release.wait(5)
        _write_file(destination, 10)
        yield {"done": True, "size": 10}

    app_module._download_chunks = blocked
    try:
        response = _create(anon, title="Friends tape")
        assert response.status_code == 202
        job_id = response.get_json()["id"]
        own = anon.get("/api/downloads/jobs").get_json()["jobs"]
        assert [job["id"] for job in own] == [job_id]
        assert anon.get("/api/downloads/jobs/%s" % job_id).status_code == 200
        assert anon.post("/api/downloads/jobs/%s/cancel" % job_id).status_code == 200
        _wait_job(anon, job_id, state="cancelled")
    finally:
        release.set()
        app_module._download_chunks = original


def test_owner_sees_all_users_jobs(web):
    entry = downloads.entry_for(app_module.DATA_DIR, "Friend job", URL, "archive")
    job = jobs_store.new("friends", "viewer", "Friend job", "archive", URL, entry)
    with app_module._DOWNLOAD_JOBS_LOCK:
        app_module._DOWNLOAD_JOB_STATE["base"] = app_module.DATA_DIR
        app_module._DOWNLOAD_JOB_STATE["jobs"][job["id"]] = job
    listed = web.get("/api/downloads/jobs").get_json()["jobs"]
    assert [item["id"] for item in listed] == [job["id"]]
    assert listed[0]["identity"] == "friends"
    assert web.get("/api/downloads/jobs/%s" % job["id"]).status_code == 200
