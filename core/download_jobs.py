# -*- coding: utf-8 -*-
"""Shared background-download job store (SQLite, 1.6.28).

The job table lives in the same WAL database as auth sessions/audit
(``RAVEWEB_AUTH_DB`` / ``$RAVEWEB_DATA/auth.sqlite3``).  Both gunicorn workers
see one row set and use ``BEGIN IMMEDIATE`` transactions for transitions, so a
poll can never land on a worker that did not create the job.  The in-process
worker (``app.py``) only claims rows; it never owns the state in memory.
"""

import json
import os
import re
import secrets
import time

from . import audit, downloads

MAX_JOBS = 20
STATES = ("queued", "downloading", "completed", "failed", "cancelled")
JOB_ID_RE = re.compile(r"^[a-f0-9]{24}$")
TEMP_PREFIX = ".tmp-"

_ENTRY_KEYS = ("id", "title", "filename", "source", "url", "size", "created")


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


def _json_entry(entry):
    return json.dumps(entry or {}, ensure_ascii=False)


def _row_to_job(row):
    if row is None:
        return None
    try:
        entry = json.loads(row["entry_json"] or "{}")
    except (TypeError, ValueError):
        entry = {}
    return {
        "id": row["id"],
        "identity": row["identity"],
        "role": row["role"],
        "title": row["title"],
        "source": row["source"],
        "state": row["state"],
        "bytes_done": max(0, int(row["bytes_done"] or 0)),
        "bytes_total": (None if row["bytes_total"] is None
                        else max(0, int(row["bytes_total"]))),
        "progress": max(0, min(100, int(row["progress"] or 0))),
        "error": row["error"] or "",
        "created": float(row["created"] or 0),
        "updated": float(row["updated"] or 0),
        "started": row["started"],
        "finished": row["finished"],
        "download_id": row["download_id"] or "",
        "cancel_requested": bool(row["cancel_requested"]),
        "worker_pid": row["worker_pid"],
        "entry": entry if isinstance(entry, dict) else {},
    }


def public(job):
    return {
        "id": job["id"], "identity": job["identity"], "role": job["role"],
        "title": job["title"], "source": job["source"], "state": job["state"],
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


def temp_path(base_dir, job):
    entry = job.get("entry") or {}
    return os.path.join(downloads.directory(base_dir, job.get("identity")),
                        TEMP_PREFIX + str(job.get("id") or "job") + "-"
                        + str(entry.get("filename") or "download"))


def create(identity, role, title, source, url, entry):
    now = time.time()
    job = {
        "id": secrets.token_hex(12), "identity": identity, "role": role,
        "title": title, "source": source, "state": "queued",
        "bytes_done": 0, "bytes_total": None, "progress": 0, "error": "",
        "created": now, "updated": now, "started": None, "finished": None,
        "download_id": "", "cancel_requested": False, "worker_pid": None,
        "entry": _valid_entry(entry) or {},
    }
    connect = audit._connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO download_jobs (id,identity,role,title,source,state,"
            "bytes_done,bytes_total,progress,error,created,updated,started,"
            "finished,download_id,cancel_requested,worker_pid,heartbeat,entry_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["identity"], job["role"], job["title"],
             job["source"], "queued", 0, None, 0, "", now, now, None, None,
             "", 0, None, now, _json_entry(job["entry"])))
        conn.commit()
    return job


def get(job_id):
    if not isinstance(job_id, str) or not job_id:
        return None
    with audit._connect() as conn:
        row = conn.execute("SELECT * FROM download_jobs WHERE id=?",
                           (job_id,)).fetchone()
    return _row_to_job(row)


def list_jobs(identity=None, role=None):
    with audit._connect() as conn:
        if role == "admin":
            rows = conn.execute(
                "SELECT * FROM download_jobs ORDER BY created DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM download_jobs WHERE identity=? ORDER BY created DESC",
                (identity or "",)).fetchall()
    return [_row_to_job(row) for row in rows]


def list_for(identity, role):
    return list_jobs(identity, role)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def recover_orphans():
    """Fail active rows whose worker process no longer exists."""
    now = time.time()
    with audit._connect() as conn:
        rows = conn.execute(
            "SELECT id,state,worker_pid FROM download_jobs "
            "WHERE state IN ('queued','downloading')").fetchall()
        for row in rows:
            if row["state"] == "downloading" and not _pid_alive(row["worker_pid"]):
                conn.execute(
                    "UPDATE download_jobs SET state='failed', error=?, finished=?, "
                    "updated=?, cancel_requested=1 WHERE id=? AND state='downloading'",
                    ("interrupted by server restart", now, now, row["id"]))
        conn.commit()


def claim_next(max_concurrency=2):
    """Atomically claim the oldest queued job if a global slot is free."""
    now = time.time()
    conn = audit._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM download_jobs WHERE state='downloading'"
        ).fetchone()["n"]
        if active >= int(max_concurrency):
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT id FROM download_jobs WHERE state='queued' "
            "AND cancel_requested=0 ORDER BY created LIMIT 1").fetchone()
        if row is None:
            conn.rollback()
            return None
        changed = conn.execute(
            "UPDATE download_jobs SET state='downloading', started=?, updated=?, "
            "worker_pid=?, heartbeat=? WHERE id=? AND state='queued'",
            (now, now, os.getpid(), now, row["id"])).rowcount
        if changed != 1:
            conn.rollback()
            return None
        conn.commit()
        result = conn.execute("SELECT * FROM download_jobs WHERE id=?",
                              (row["id"],)).fetchone()
        return _row_to_job(result)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def update(job_id, **fields):
    allowed = {"bytes_done", "bytes_total", "progress", "error", "state",
               "download_id", "finished", "started", "cancel_requested"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append("%s=?" % key)
        params.append(value)
    sets.append("updated=?")
    params.append(time.time())
    params.append(job_id)
    with audit._connect() as conn:
        conn.execute("UPDATE download_jobs SET " + ", ".join(sets) + " WHERE id=?",
                     params)
        conn.commit()


def heartbeat(job_id, **fields):
    sets, params = [], []
    for key in ("bytes_done", "bytes_total", "progress"):
        if key in fields:
            sets.append("%s=?" % key)
            params.append(fields[key])
    sets.extend(["heartbeat=?", "updated=?"])
    now = time.time()
    params.extend([now, now, job_id])
    with audit._connect() as conn:
        conn.execute("UPDATE download_jobs SET " + ", ".join(sets) + " WHERE id=?",
                     params)
        conn.commit()


def cancel(job_id, identity=None, role=None):
    """Atomically cancel an active job owned by identity (owner may cancel any)."""
    now = time.time()
    conn = audit._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM download_jobs WHERE id=?",
                           (job_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        if role != "admin" and row["identity"] != (identity or ""):
            conn.rollback()
            return None
        if row["state"] not in ("queued", "downloading"):
            conn.rollback()
            return _row_to_job(row)
        conn.execute(
            "UPDATE download_jobs SET state='cancelled', cancel_requested=1, "
            "finished=?, updated=? WHERE id=? AND state IN ('queued','downloading')",
            (now, now, job_id))
        conn.commit()
        result = conn.execute("SELECT * FROM download_jobs WHERE id=?",
                              (job_id,)).fetchone()
        return _row_to_job(result)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def complete(job_id, size, download_id):
    """Atomically finish a job only if it is still downloading and not cancelled."""
    now = time.time()
    with audit._connect() as conn:
        changed = conn.execute(
            "UPDATE download_jobs SET state='completed', bytes_done=?, "
            "bytes_total=?, progress=100, finished=?, updated=?, download_id=?, "
            "error='' WHERE id=? AND state='downloading' AND cancel_requested=0",
            (int(size or 0), int(size or 0), now, now, download_id or "", job_id)
        ).rowcount
        conn.commit()
    return changed > 0


def fail(job_id, error, state="failed"):
    now = time.time()
    with audit._connect() as conn:
        conn.execute(
            "UPDATE download_jobs SET state=?, error=?, finished=?, updated=?, "
            "cancel_requested=1 WHERE id=? AND state IN ('queued','downloading')",
            (state if state in ("failed", "cancelled") else "failed",
             str(error or "")[:300], now, now, job_id))
        conn.commit()
    return True


def cancel_requested(job_id):
    job = get(job_id)
    return bool(job and (job.get("cancel_requested")
                         or job.get("state") == "cancelled"))


def clear_all():
    """Test/operator cleanup; not exposed over HTTP."""
    with audit._connect() as conn:
        conn.execute("DELETE FROM download_jobs")
        conn.commit()


def migrate_from_legacy_json(base_dir):
    """One-way migration for the short-lived per-worker jobs.json (1.6.23)."""
    path = os.path.join(downloads.directory(base_dir), "jobs.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, TypeError):
        return 0
    migrated = 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not JOB_ID_RE.match(str(item.get("id") or "")):
            continue
        entry = _valid_entry(item.get("entry"))
        if entry is None:
            continue
        job = create(item.get("identity") or "owner", item.get("role") or "viewer",
                     item.get("title") or "Download", item.get("source") or "",
                     entry.get("url") or "", entry)
        update(job["id"], state=item.get("state") if item.get("state") in STATES else "failed",
               bytes_done=item.get("bytes_done") or 0,
               bytes_total=item.get("bytes_total"),
               progress=item.get("progress") or 0,
               error=item.get("error") or "",
               finished=item.get("finished") or time.time())
        migrated += 1
    try:
        os.remove(path)
    except OSError:
        pass
    return migrated
