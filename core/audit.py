# -*- coding: utf-8 -*-
"""Persistent audit, session and block store for the owner/admin surface.

SQLite lives beside the favourites/cache data (default
``$RAVEWEB_DATA/auth.sqlite3``; override with ``RAVEWEB_AUTH_DB``).  It is a
single small file with WAL enabled, so the two gunicorn workers can share it.

The store is deliberately boring: one connection per operation, bounded text
fields, no ORM.  If it is unreadable the auth layer fails closed rather than
silently allowing a session.
"""

import calendar
import hashlib
import ipaddress
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing

_DB_PATH = None
_READY = set()
_LOCK = threading.Lock()
_MAX_TEXT = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    role TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    remember INTEGER NOT NULL DEFAULT 0,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(revoked_at, expires_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    identity TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event, ts);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    until REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_blocks_until ON blocks(until);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def configure(path=None):
    """Point the store at a path (tests use a temp file per case)."""
    global _DB_PATH
    _DB_PATH = str(path) if path else None
    _READY.clear()


def db_path():
    return _DB_PATH or os.path.join(
        os.environ.get("RAVEWEB_DATA", "/tmp/raveweb"), "auth.sqlite3")


def _text(value, limit=_MAX_TEXT):
    return str(value or "")[:limit]


def _connect():
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    with _LOCK:
        if path not in _READY:
            conn.executescript(_SCHEMA)
            conn.commit()
            _READY.add(path)
    return conn


def _write(sql, params=()):
    with closing(_connect()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def _read(sql, params=()):
    with closing(_connect()) as conn:
        return conn.execute(sql, params).fetchall()


def record_event(event, identity="", role="", ip="", user_agent="", endpoint="",
                 bytes=0, status=0, ts=None):
    _write(
        "INSERT INTO events (ts,event,identity,role,ip,user_agent,endpoint,bytes,status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (float(ts if ts is not None else time.time()), _text(event, 64),
         _text(identity, 64), _text(role, 16), _text(ip, 64),
         _text(user_agent, 300), _text(endpoint, 300), int(bytes or 0),
         int(status or 0)))


def create_session(identity, role, ip, user_agent, ttl_seconds, remember=False):
    now = time.time()
    sid = secrets.token_urlsafe(32)
    _write(
        "INSERT INTO sessions (id,identity,role,ip,user_agent,created_at,last_seen,"
        "expires_at,remember) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, _text(identity, 64), _text(role, 16), _text(ip, 64),
         _text(user_agent, 300), now, now, now + float(ttl_seconds),
         1 if remember else 0))
    return sid


def session_active(sid):
    if not sid:
        return False
    now = time.time()
    rows = _read(
        "SELECT 1 FROM sessions WHERE id=? AND revoked_at IS NULL AND expires_at>?",
        (str(sid), now))
    return bool(rows)


def touch_session(sid, ip="", user_agent=""):
    if not sid:
        return
    _write("UPDATE sessions SET last_seen=?, ip=?, user_agent=? WHERE id=?",
           (time.time(), _text(ip, 64), _text(user_agent, 300), str(sid)))


def revoke_session(sid):
    return _write(
        "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (time.time(), str(sid))) > 0


def revoke_all_sessions():
    """Invalidate every session row (owner-compromise response; no HTTP route)."""
    return _write("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL",
                  (time.time(),))


def revoke_sessions(identity=None, ip=None):
    if not identity and not ip:
        return 0
    now = time.time()
    clauses = ["revoked_at IS NULL", "expires_at>" + "?"]
    params = [now]
    if identity:
        clauses.append("identity=?")
        params.append(_text(identity, 64))
    if ip:
        clauses.append("ip=?")
        params.append(_text(ip, 64))
    return _write("UPDATE sessions SET revoked_at=? WHERE " + " AND ".join(clauses),
                 [now] + params)


def list_sessions():
    now = time.time()
    rows = _read(
        "SELECT id,identity,role,ip,user_agent,created_at,last_seen,expires_at,remember "
        "FROM sessions WHERE revoked_at IS NULL AND expires_at>? "
        "ORDER BY last_seen DESC", (now,))
    return [dict(row) for row in rows]


def add_block(identity=None, ip=None, seconds=900, reason="", created_by="owner"):
    identity = _text(identity, 64)
    ip = _text(ip, 64)
    if not identity and not ip:
        raise ValueError("identity or ip is required")
    now = time.time()
    until = now + max(1.0, min(float(seconds or 900), 86400 * 30))
    _write("INSERT INTO blocks (identity,ip,created_at,until,reason,created_by) "
           "VALUES (?,?,?,?,?,?)",
           (identity, ip, now, until, _text(reason, 300), _text(created_by, 64)))
    return until


def list_blocks():
    rows = _read("SELECT id,identity,ip,created_at,until,reason,created_by "
                 "FROM blocks WHERE until>? ORDER BY until DESC", (time.time(),))
    return [dict(row) for row in rows]


def is_blocked(identity="", ip=""):
    now = time.time()
    rows = _read(
        "SELECT 1 FROM blocks WHERE until>? AND ((identity<>'' AND identity=?) "
        "OR (ip<>'' AND ip=?)) LIMIT 1",
        (now, _text(identity, 64), _text(ip, 64)))
    return bool(rows)


def delete_blocks(identity=None, ip=None):
    if not identity and not ip:
        return 0
    clauses = ["until>?"]
    params = [time.time()]
    if identity:
        clauses.append("identity=?")
        params.append(_text(identity, 64))
    if ip:
        clauses.append("ip=?")
        params.append(_text(ip, 64))
    return _write("DELETE FROM blocks WHERE " + " AND ".join(clauses), params)


def get_setting(key, default=None):
    rows = _read("SELECT value FROM settings WHERE key=?", (str(key),))
    return rows[0]["value"] if rows else default


def set_setting(key, value):
    _write("INSERT INTO settings (key,value) VALUES (?,?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           (str(key), str(value)))


def delete_setting(key):
    return _write("DELETE FROM settings WHERE key=?", (str(key),))


def _count(event, start=None, end=None):
    if start is None:
        rows = _read("SELECT COUNT(*) AS n FROM events WHERE event=?", (event,))
    else:
        rows = _read("SELECT COUNT(*) AS n FROM events WHERE event=? AND ts>=?",
                     (event, start))
    return int(rows[0]["n"])


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _today_start(now):
    stamp = time.gmtime(now)
    return calendar.timegm((stamp.tm_year, stamp.tm_mon, stamp.tm_mday,
                            0, 0, 0, 0, 0, 0))


def stats(window_hours=24, top_limit=10):
    now = time.time()
    start = now - float(window_hours) * 3600.0
    active = list_sessions()
    sessions_now = len(active)
    owner_now = sum(1 for item in active if item.get("identity") == "owner")
    friends_now = sum(1 for item in active if item.get("identity") == "friends")
    unique_ips = _read(
        "SELECT COUNT(DISTINCT ip) AS n FROM events WHERE ts>=? AND ip<>''",
        (start,))[0]["n"]
    requests = _read(
        "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b FROM events "
        "WHERE ts>=? AND event='request'", (start,))[0]
    logins = _count("login_success", start)
    failures = _count("login_failure", start)
    lockouts = _count("login_locked", start) + _count("login_blocked", start)
    today = _today_start(now)
    today_counts = {
        "logins_today": _count("login_success", today),
        "failures_today": _count("login_failure", today),
        "lockouts_today": (_count("login_locked", today)
                            + _count("login_blocked", today)),
    }
    top = _read(
        "SELECT endpoint, COUNT(*) AS requests, COALESCE(SUM(bytes),0) AS bytes "
        "FROM events WHERE ts>=? AND event='request' AND endpoint<>'' "
        "GROUP BY endpoint ORDER BY requests DESC, endpoint LIMIT ?",
        (start, int(top_limit)))
    by_day = {}
    day_rows = _read(
        "SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS day, "
        "COALESCE(SUM(bytes),0) AS bytes FROM events "
        "WHERE ts>=? AND event='request' GROUP BY day", (start,))
    for row in day_rows:
        by_day[row["day"]] = int(row["bytes"] or 0)
    days = []
    for offset in range(6, -1, -1):
        stamp = now - offset * 86400
        key = time.strftime("%Y-%m-%d", time.gmtime(stamp))
        days.append({"date": key, "bytes": by_day.get(key, 0)})
    last_ips = _read(
        "SELECT e.ip AS ip, MAX(e.ts) AS last_seen, "
        "(SELECT e2.identity FROM events e2 WHERE e2.ip=e.ip "
        " ORDER BY e2.ts DESC LIMIT 1) AS identity "
        "FROM events e WHERE e.ts>=? AND e.ip<>'' "
        "GROUP BY e.ip ORDER BY last_seen DESC LIMIT 20", (start,))
    session_items = [
        # Never expose the raw session id: it is a bearer-equivalent secret.
        # A short SHA-256 prefix is stable for the UI but not reversible.
        {"session_id_masked":
             hashlib.sha256(item["id"].encode("utf-8")).hexdigest()[:10] + "…",
         "identity": item["identity"], "role": item["role"],
         "ip": item["ip"], "created": _iso(item["created_at"]),
         "last_seen": _iso(item["last_seen"]),
         "expires_at": _iso(item["expires_at"])}
        for item in active
    ]
    return {
        "status": "ok",
        "generated_at": _iso(now),
        "updated": _iso(now),
        "window_hours": window_hours,
        "sessions": {
            "now": sessions_now,
            "online_owner": owner_now,
            "online_friends": friends_now,
            "items": session_items,
        },
        "sessions_now": sessions_now,
        "unique_ips_24h": int(unique_ips or 0),
        "logins_24h": logins,
        "failures_24h": failures,
        "lockouts_24h": lockouts,
        "logins_today": today_counts["logins_today"],
        "failures_today": today_counts["failures_today"],
        "lockouts_today": today_counts["lockouts_today"],
        "requests_24h": int(requests["n"] or 0),
        "bytes_24h": int(requests["b"] or 0),
        "bandwidth": {
            "bytes_in": 0,
            "bytes_out": int(requests["b"] or 0),
            "window": "24h",
            "window_hours": window_hours,
            "by_day": days,
        },
        "bandwidth_by_day": days,
        "top_endpoints": [{"endpoint": row["endpoint"],
                           "requests": int(row["requests"]),
                           "bytes": int(row["bytes"] or 0)}
                          for row in top],
        "last_ips": [{"ip": row["ip"], "last_seen": _iso(row["last_seen"]),
                      "identity": row["identity"] or ""}
                     for row in last_ips],
    }


def prune(max_age_days=30):
    cutoff = time.time() - max_age_days * 86400
    _write("DELETE FROM events WHERE ts<?", (cutoff,))
    _write("DELETE FROM blocks WHERE until<?", (time.time(),))
    _write("DELETE FROM sessions WHERE expires_at<?", (cutoff,))


def valid_ip(value):
    try:
        ipaddress.ip_address(str(value))
        return True
    except ValueError:
        return False
