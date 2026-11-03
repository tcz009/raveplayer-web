# -*- coding: utf-8 -*-
"""Owner-only playback presence/history (1.6.34).

The browser sends a best-effort heartbeat (play/pause/stop/seek and every 30s)
to ``/api/playback``.  Rows live in the auth SQLite database (WAL) so both
gunicorn workers and the owner admin views see the same state.  Only labels,
source keys, positions and timestamps are stored — never URLs, tokens or
session cookies.  A heartbeat older than ``STALE_AFTER`` seconds is presented
as stopped.
"""

import datetime
import os
import time

from . import audit

STALE_AFTER = 90.0
DEFAULT_RETENTION_DAYS = 7.0
DEFAULT_EVENT_LIMIT = 5000
DEFAULT_HISTORY_LIMIT = 500
STATES = ("playing", "paused", "stopped")


def _env_float(name, default, minimum=0.0):
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_int(name, default, minimum=1):
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def retention_days():
    return _env_float("RAVEWEB_PLAYBACK_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 0.1)


def event_limit():
    return _env_int("RAVEWEB_PLAYBACK_EVENT_LIMIT", DEFAULT_EVENT_LIMIT, 1)


def _iso(ts):
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _prune(conn, now):
    cutoff = now - retention_days() * 86400.0
    conn.execute("DELETE FROM playback_events WHERE ts<?", (cutoff,))
    limit = event_limit()
    conn.execute(
        "DELETE FROM playback_events WHERE id NOT IN "
        "(SELECT id FROM playback_events ORDER BY id DESC LIMIT ?)", (limit,))


def record(identity, session_id, role, state, label, source, position, duration,
           client_ts=None, now=None):
    """Upsert now-playing and append an event only when the state changes."""
    if state not in STATES or not identity or not session_id:
        raise ValueError("invalid playback heartbeat")
    now = time.time() if now is None else float(now)
    with audit._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT state,label,source FROM playback_now "
            "WHERE identity=? AND session_id=?",
            (identity, session_id)).fetchone()
        changed = (previous is None or previous["state"] != state
                   or previous["label"] != label or previous["source"] != source)
        conn.execute(
            "INSERT INTO playback_now (identity,session_id,role,state,label,source,"
            "position,duration,client_ts,updated) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(identity,session_id) DO UPDATE SET "
            "role=excluded.role,state=excluded.state,label=excluded.label,"
            "source=excluded.source,position=excluded.position,"
            "duration=excluded.duration,client_ts=excluded.client_ts,"
            "updated=excluded.updated",
            (identity, session_id, role or "", state, label or "", source or "",
             float(position or 0), duration, client_ts, now))
        if changed:
            conn.execute(
                "INSERT INTO playback_events (ts,identity,session_id,state,label,"
                "source,position,duration) VALUES (?,?,?,?,?,?,?,?)",
                (now, identity, session_id, state, label or "", source or "",
                 float(position or 0), duration))
        _prune(conn, now)
        conn.commit()
    if changed:
        try:
            audit.record_event(
                "play_event", identity=identity, role=role or "",
                endpoint="/api/playback", status=204,
                detail="state=%s source=%s" % (state, source or "-"))
        except Exception:
            pass  # presence auditing is best effort
    return changed


def _now_row(row, now):
    age = max(0.0, now - float(row["updated"]))
    fresh = age <= STALE_AFTER
    state = row["state"] if fresh else "stopped"
    return {
        "identity": row["identity"],
        "session_id_masked": audit.masked_session_id(row["session_id"]),
        "state": state,
        "label": row["label"] or "",
        "source": row["source"] or "",
        "position": max(0.0, float(row["position"] or 0)),
        "duration": (None if row["duration"] is None else max(0.0, float(row["duration"]))),
        "updated": _iso(row["updated"]),
        "age_seconds": round(age, 1),
        "online": bool(fresh and state != "stopped"),
    }


def now_playing(now=None):
    now = time.time() if now is None else float(now)
    with audit._connect() as conn:
        rows = conn.execute(
            "SELECT p.* FROM playback_now p WHERE p.updated = "
            "(SELECT MAX(p2.updated) FROM playback_now p2 WHERE p2.identity=p.identity) "
            "ORDER BY p.identity").fetchall()
    return [_now_row(row, now) for row in rows]


def history(hours=24, identity=None, limit=None):
    hours = max(1.0, min(float(hours or 24), 24 * 7))
    limit = int(limit or DEFAULT_HISTORY_LIMIT)
    start = time.time() - hours * 3600.0
    with audit._connect() as conn:
        if identity:
            rows = conn.execute(
                "SELECT ts,identity,session_id,state,label,source,position,duration "
                "FROM playback_events WHERE ts>=? AND identity=? "
                "ORDER BY ts DESC LIMIT ?", (start, identity, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts,identity,session_id,state,label,source,position,duration "
                "FROM playback_events WHERE ts>=? "
                "ORDER BY ts DESC LIMIT ?", (start, limit)).fetchall()
    return [{
        "at": _iso(row["ts"]),
        "identity": row["identity"],
        "session_id_masked": audit.masked_session_id(row["session_id"]),
        "event": "play_event",
        "state": row["state"],
        "label": row["label"] or "",
        "source": row["source"] or "",
        "position": max(0.0, float(row["position"] or 0)),
        "duration": (None if row["duration"] is None else max(0.0, float(row["duration"]))),
    } for row in rows]


def export_payload(hours=24, identity=None):
    hours = max(1.0, min(float(hours or 24), 24 * 7))
    return {
        "kind": "rave-playback-log",
        "exported_at": _iso(time.time()),
        "hours": hours,
        "stale_after_seconds": STALE_AFTER,
        "now": now_playing(),
        "history": history(hours, identity),
    }
