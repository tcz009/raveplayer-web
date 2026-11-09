# -*- coding: utf-8 -*-
"""Shared cancellation state for foreground (NDJSON) downloads.

The progress panel sends ``client_id`` with the streaming ``POST
/api/downloads`` call and may cancel from a different gunicorn worker.  An
in-process set therefore cannot work: the cancel POST can land on worker B
while worker A is streaming the file.

This module keeps one small row per client id in the same WAL SQLite database
as auth sessions and background jobs, so every worker sees the same state:

``active`` -> the response generator owns the transfer
``cancelled`` -> a cancel POST has been accepted; the downloader must stop
``finished`` -> the transfer ended (saved or failed); cancel is a 409

Rows are short lived: ``ACTIVE_TTL`` covers the longest download deadline and
``DONE_TTL`` keeps a terminal state long enough for a late/duplicate cancel to
get a useful 409/200 rather than a misleading 404.  ``begin`` deliberately
preserves a recent ``cancelled`` row: a cancel that wins the race before the
streaming response starts must not be erased by the generator's own begin.
Client ids are single-use random tokens (6-64 chars), so that is safe.
"""

import time

from . import audit

ACTIVE_TTL = 6 * 60 * 60
DONE_TTL = 60 * 60

STATES = ("active", "cancelled", "finished")


def _now():
    return time.time()


def _prune(conn, now):
    conn.execute("DELETE FROM download_cancels WHERE expires < ?", (now,))


def prune():
    """Drop expired rows; safe to call from request/worker paths."""
    now = _now()
    with audit._connect() as conn:
        _prune(conn, now)
        conn.commit()


def begin(client_id):
    """Mark a foreground transfer as active.

    Returns ``"active"`` normally, or ``"cancelled"`` when a fresh cancel
    request already exists for this id (the downloader must stop immediately).
    """
    if not client_id:
        return "active"
    now = _now()
    with audit._connect() as conn:
        _prune(conn, now)
        row = conn.execute(
            "SELECT state,expires FROM download_cancels WHERE client_id=?",
            (client_id,)).fetchone()
        if row is not None and row["state"] == "cancelled" and row["expires"] > now:
            conn.execute(
                "UPDATE download_cancels SET expires=? WHERE client_id=?",
                (now + DONE_TTL, client_id))
            conn.commit()
            return "cancelled"
        conn.execute(
            "INSERT OR REPLACE INTO download_cancels "
            "(client_id,state,created,updated,expires) VALUES (?,?,?,?,?)",
            (client_id, "active", now, now, now + ACTIVE_TTL))
        conn.commit()
        return "active"


def request_cancel(client_id):
    """Durably request cancellation.

    Returns ``"cancelled"`` (accepted, or already accepted), ``"finished"``
    (the transfer already ended) or ``"unknown"`` (no such transfer).
    """
    if not client_id:
        return "unknown"
    now = _now()
    conn = audit._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state,expires FROM download_cancels WHERE client_id=?",
            (client_id,)).fetchone()
        if row is None:
            conn.rollback()
            return "unknown"
        if row["state"] == "finished":
            conn.rollback()
            return "finished"
        if row["state"] == "cancelled":
            conn.execute(
                "UPDATE download_cancels SET expires=?,updated=? WHERE client_id=?",
                (now + DONE_TTL, now, client_id))
            conn.commit()
            return "cancelled"
        conn.execute(
            "UPDATE download_cancels SET state='cancelled',updated=?,expires=? "
            "WHERE client_id=? AND state='active'",
            (now, now + DONE_TTL, client_id))
        conn.commit()
        return "cancelled"
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def is_cancelled(client_id):
    """True when the shared store says this transfer must stop."""
    if not client_id:
        return False
    with audit._connect() as conn:
        row = conn.execute(
            "SELECT state FROM download_cancels WHERE client_id=?",
            (client_id,)).fetchone()
    return bool(row is not None and row["state"] == "cancelled")


def finish(client_id):
    """Mark a transfer terminal.

    Returns ``True`` when it was still active (now finished), ``False`` when a
    cancel won the race (the caller must not publish the file), or ``None``
    when no row exists (direct ``_download_stream`` callers/tests).
    """
    if not client_id:
        return None
    now = _now()
    conn = audit._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state FROM download_cancels WHERE client_id=?",
            (client_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        if row["state"] == "cancelled":
            conn.commit()
            return False
        if row["state"] == "finished":
            conn.commit()
            return True
        conn.execute(
            "UPDATE download_cancels SET state='finished',updated=?,expires=? "
            "WHERE client_id=? AND state='active'",
            (now, now + DONE_TTL, client_id))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def clear(client_id):
    """Remove one row (test/operator cleanup; not exposed over HTTP)."""
    if not client_id:
        return
    with audit._connect() as conn:
        conn.execute("DELETE FROM download_cancels WHERE client_id=?", (client_id,))
        conn.commit()


def clear_all():
    """Remove every row (tests)."""
    with audit._connect() as conn:
        conn.execute("DELETE FROM download_cancels")
        conn.commit()
