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
    job_state = jobs_store.get(job_id)
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
    job = jobs_store.create("owner", "admin", "Interrupted", "archive", URL, entry)
    jobs_store.update(job["id"], state="downloading", bytes_done=10)
    # Simulate a worker process that has died: no live pid on the row.
    with jobs_store.audit._connect() as conn:
        conn.execute("UPDATE download_jobs SET worker_pid=? WHERE id=?",
                     (999999, job["id"]))
        conn.commit()
    app_module._jobs_ensure()
    stored = jobs_store.get(job["id"])
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
    job = jobs_store.create("friends", "viewer", "Friend job", "archive", URL, entry)
    listed = web.get("/api/downloads/jobs").get_json()["jobs"]
    assert [item["id"] for item in listed] == [job["id"]]
    assert listed[0]["identity"] == "friends"
    assert web.get("/api/downloads/jobs/%s" % job["id"]).status_code == 200


def _spawn_job(db_path, title, url):
    code = (
        "from core import audit, download_jobs, downloads\n"
        "audit.configure(%r)\n"
        "entry = downloads.entry_for('.', %r, %r, 'archive')\n"
        "job = download_jobs.create('owner', 'admin', %r, 'archive', %r, entry)\n"
        "print(job['id'])\n" % (db_path, title, url, title, url)
    )
    import subprocess
    import sys
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.Popen([sys.executable, "-c", code], cwd=env["PYTHONPATH"],
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def test_shared_store_across_processes_no_clobber():
    from core import audit
    db_path = audit.db_path()
    # Initialise the schema before the two writers race.
    assert jobs_store.list_for("owner", "admin") == []
    one = _spawn_job(db_path, "process one", "https://archive.org/download/a/one.mp3")
    two = _spawn_job(db_path, "process two", "https://archive.org/download/a/two.mp3")
    out_one, err_one = one.communicate(timeout=120)
    out_two, err_two = two.communicate(timeout=120)
    assert one.returncode == 0, err_one
    assert two.returncode == 0, err_two
    ids = {out_one.strip(), out_two.strip()}
    assert len(ids) == 2
    jobs = jobs_store.list_for("owner", "admin")
    assert {job["id"] for job in jobs} == ids
    # A claim/complete in this process is visible to the same shared rows, and
    # cancelling the other job leaves it intact.
    claimed = jobs_store.claim_next(2)
    assert claimed and claimed["id"] in ids
    assert jobs_store.complete(claimed["id"], 4, "download-" + claimed["id"])
    other = next(job for job in jobs if job["id"] != claimed["id"])
    cancelled = jobs_store.cancel(other["id"], "owner", "admin")
    assert cancelled["state"] == "cancelled"
    assert jobs_store.get(claimed["id"])["state"] == "completed"
    assert jobs_store.get(other["id"])["state"] == "cancelled"
