# -*- coding: utf-8 -*-
"""Persistent background-download job records.

The worker lives in ``app.py`` (it reuses the SSRF-hardened download proxy); this
module only validates/persists job metadata beside the Downloads index.  Jobs
are JSON so the two gunicorn workers and a restart see the same state; atomic
replace keeps a crash from truncating the file.
"""

import json
import os
import re
import secrets
import tempfile
import time

from . import downloads

JOBSNAME = "jobs.json"
TEMP_PREFIX = ".tmp-"
MAX_JOBS = 20
STATES = ("queued", "downloading", "completed", "failed", "cancelled")
JOB_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_ENTRY_KEYS = ("id", "title", "filename", "source", "url", "size", "created")


def path(base_dir):
    return os.path.join(downloads.directory(base_dir), JOBSNAME)


def _valid_entry(entry):
    if not isinstance(entry, dict):
        return None
    cleaned = {}
    for key in _ENTRY_KEYS:
        value = entry.get(key)
        if key in ("size", "created"):
            try:
                cleaned[key] = float(value or 0)
            except (TypeError, ValueError):
                cleaned[key] = 0.0
        else:
            cleaned[key] = str(value or "")[:300]
    if not cleaned["id"] or not cleaned["filename"]:
        return None
    if not downloads._valid_filename(cleaned["filename"]):
        return None
    return cleaned


def _clean_job(raw):
    if not isinstance(raw, dict):
        return None
    identifier = str(raw.get("id") or "")
    if not JOB_ID_RE.match(identifier):
        return None
    state = str(raw.get("state") or "queued")
    if state not in STATES:
        state = "failed"
    entry = _valid_entry(raw.get("entry"))
    if entry is None:
        return None
    job = {
        "id": identifier,
        "identity": str(raw.get("identity") or "")[:64],
        "role": str(raw.get("role") or "")[:16],
        "title": str(raw.get("title") or "Download")[:300],
        "source": str(raw.get("source") or "")[:32],
        "state": state,
        "bytes_done": max(0, int(raw.get("bytes_done") or 0)),
        "bytes_total": (None if raw.get("bytes_total") is None
                        else max(0, int(raw.get("bytes_total") or 0))),
        "progress": max(0, min(100, int(raw.get("progress") or 0))),
        "error": str(raw.get("error") or "")[:300],
        "created": float(raw.get("created") or 0),
        "updated": float(raw.get("updated") or 0),
        "started": (None if raw.get("started") is None else float(raw.get("started") or 0)),
        "finished": (None if raw.get("finished") is None else float(raw.get("finished") or 0)),
        "download_id": str(raw.get("download_id") or "")[:64],
        "cancel_requested": bool(raw.get("cancel_requested")),
        "entry": entry,
    }
    return job


def load(base_dir):
    folder = downloads.directory(base_dir)
    try:
        with open(path(base_dir), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    jobs = []
    for item in raw if isinstance(raw, list) else []:
        job = _clean_job(item)
        if job is not None:
            jobs.append(job)
    return jobs


def save(base_dir, jobs):
    folder = downloads.directory(base_dir)
    try:
        os.makedirs(folder, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder,
            prefix=".tmp-jobs-", suffix=".json", delete=False)
        try:
            json.dump(jobs, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        finally:
            handle.close()
        os.replace(temp_name, path(base_dir))
        return True
    except OSError:
        return False


def temp_path(base_dir, job):
    """Unique partial path; foreground and background jobs can share a URL."""
    entry = job.get("entry") or {}
    return os.path.join(downloads.directory(base_dir, job.get("identity")),
                        TEMP_PREFIX + str(job.get("id") or "job") + "-"
                        + str(entry.get("filename") or "download"))


def new(identity, role, title, source, url, entry):
    now = time.time()
    return {
        "id": secrets.token_hex(12),
        "identity": str(identity or "")[:64],
        "role": str(role or "")[:16],
        "title": str(title or "Download")[:300],
        "source": str(source or "")[:32],
        "state": "queued",
        "bytes_done": 0,
        "bytes_total": None,
        "progress": 0,
        "error": "",
        "created": now,
        "updated": now,
        "started": None,
        "finished": None,
        "download_id": "",
        "cancel_requested": False,
        "entry": _valid_entry(entry) or {},
    }


def recover(base_dir):
    """Mark jobs left queued/downloading by a dead process as failed.

    Partial files are removed; completed jobs and the normal Downloads index are
    untouched.  Runs lazily on the first jobs request after a restart.
    """
    jobs = load(base_dir)
    changed = False
    for job in jobs:
        if job["state"] not in ("queued", "downloading"):
            continue
        job["state"] = "failed"
        job["error"] = "interrupted by server restart"
        job["finished"] = job["updated"] = time.time()
        job["cancel_requested"] = True
        try:
            os.remove(temp_path(base_dir, job))
        except OSError:
            pass
        changed = True
    if changed:
        save(base_dir, jobs)
    return jobs


def public(job):
    """Client-safe view; URLs and the internal entry are never exposed."""
    return {
        "id": job["id"],
        "identity": job["identity"],
        "role": job["role"],
        "title": job["title"],
        "source": job["source"],
        "state": job["state"],
        "bytes_done": int(job.get("bytes_done") or 0),
        "bytes_total": job.get("bytes_total"),
        "progress": int(job.get("progress") or 0),
        "error": job.get("error") or "",
        "created": job.get("created") or 0,
        "updated": job.get("updated") or 0,
        "started": job.get("started"),
        "finished": job.get("finished"),
        "download_id": job.get("download_id") or "",
    }
