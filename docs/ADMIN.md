# Owner stats API — 1.6.22

Read-only monitoring for the Control Centre Rave Player widget. There is **no
new UI** in Rave Player and **no mutation endpoint** in this phase (session
revoke, IP/identity blocks and friends password disable/rotate are deferred to
phase 2 and deliberately not routed yet).

## Authentication

`GET /api/admin/stats` accepts either:

- an **owner session** (role `admin`); friends (viewer) get `403`; no session
  gets `401`; or
- `Authorization: Bearer $RAVEWEB_WIDGET_TOKEN` — read-only, no session/cookie
  required. If `RAVEWEB_WIDGET_TOKEN` is unset/empty, bearer access is off.

The token only unlocks this one read-only route. It cannot reach `/api/admin/whoami`,
sessions, blocks, friends actions or anything else, and it never gets a session
cookie.

Environment:

| Variable | Default | Purpose |
|---|---|---|
| `RAVEWEB_WIDGET_TOKEN` | empty | Bearer token for `GET /api/admin/stats`; generate a long random value and keep it in the Control Centre secret/env only. Empty = owner-session only. |
| `RAVEWEB_AUTH_DB` | `$RAVEWEB_DATA/auth.sqlite3` | SQLite audit/session store path. Keep it on the same persistent data volume as favourites; the container's `/data` volume already covers the default. |

## Payload

```json
{
  "status": "ok",
  "generated_at": "2026-11-01T18:05:00Z",
  "updated": "2026-11-01T18:05:00Z",
  "window_hours": 24,
  "sessions": {
    "now": 2,
    "online_owner": 1,
    "online_friends": 1,
    "items": [
      {
        "id": "server-session-id",
        "identity": "owner",
        "role": "admin",
        "ip": "192.168.1.10",
        "created": "2026-11-01T17:00:00Z",
        "last_seen": "2026-11-01T18:04:00Z",
        "expires_at": "2026-11-02T05:00:00Z"
      }
    ]
  },
  "sessions_now": 2,
  "unique_ips_24h": 3,
  "logins_24h": 5,
  "failures_24h": 1,
  "lockouts_24h": 0,
  "logins_today": 3,
  "failures_today": 1,
  "lockouts_today": 0,
  "requests_24h": 120,
  "bytes_24h": 7340032,
  "bandwidth": {
    "bytes_in": 0,
    "bytes_out": 7340032,
    "window": "24h",
    "window_hours": 24,
    "by_day": [
      {"date": "2026-10-26", "bytes": 0},
      {"date": "2026-10-27", "bytes": 0},
      {"date": "2026-10-28", "bytes": 0},
      {"date": "2026-10-29", "bytes": 0},
      {"date": "2026-10-30", "bytes": 0},
      {"date": "2026-10-31", "bytes": 0},
      {"date": "2026-11-01", "bytes": 7340032}
    ]
  },
  "bandwidth_by_day": [{"date": "2026-11-01", "bytes": 7340032}],
  "top_endpoints": [
    {"endpoint": "/api/menu", "requests": 40, "bytes": 123456},
    {"endpoint": "/api/latest", "requests": 12, "bytes": 90000}
  ],
  "last_ips": [
    {"ip": "192.168.1.10", "last_seen": "2026-11-01T18:04:00Z", "identity": "owner"}
  ]
}
```

Semantics:

- `logins_24h` / `failures_24h` / `lockouts_24h` / `unique_ips_24h` /
  `requests_24h` / `bytes_24h` cover the last rolling **24 hours**.
- `failures_today` / `lockouts_today` / `logins_today` use **UTC midnight**.
- `bandwidth.bytes_in` is always `0` (the server does not meter inbound);
  `bytes_out` is the sum of response `Content-Length` for audited requests.
  `by_day` always has seven UTC date buckets (today last).
- `top_endpoints` is the top 10 request paths by request count in 24 h.
- `last_ips` is up to 20 distinct IPs seen in 24 h, newest first.
- `sessions.items[].id` is the server session id, not the cookie value.
- On an audit-DB failure the route returns `503` with
  `{"status": "down", "error": "stats unavailable"}`; the widget should show
  degraded/down rather than fabricate numbers.

Example:

```sh
curl -H "Authorization: Bearer $RAVEWEB_WIDGET_TOKEN" http://192.168.1.179:8097/api/admin/stats
curl -b raveplayer_session=... http://192.168.1.179:8097/api/admin/stats
```

## Stored data

SQLite (`sessions`, `events` tables, WAL) records:

- logins (`login_success`), failures (`login_failure`), lockouts/429s
  (`login_locked`), CSRF rejects, logouts;
- identity, role, IP, user-agent, endpoint, response bytes and status;
- one active-session row per login (identity, role, IP, UA, created/last-seen/
  expiry/remember), so `sessions_now` is server truth and revoked/expired
  sessions stop counting.

Only `/api/*`, `/`, `/login` and `/downloads*` responses are audited for
bytes/endpoints; `/static/*`, `/healthz`, the manifest and service worker are
excluded. There is no automatic retention/pruning yet (phase 2 operational
item); the file lives on the persistent data volume.

## Deferred (phase 2)

- `GET /api/admin/sessions` + `POST /api/admin/sessions/<id>/revoke`
- `POST /api/admin/blocks` / `DELETE /api/admin/blocks` by identity/IP
- friends password disable/rotate actions
- automatic retention/pruning and dashboard UI

The `core/audit.py` helpers for some of these exist as unexposed groundwork;
they have no HTTP routes in 1.6.22.

## Tests

`test/test_stats.py` (6): owner access; widget bearer read-only; wrong token
401; friends/no-auth rejection; 24h/today window aggregation (logins, failures,
lockouts, unique IPs, bytes, top endpoints, last IPs, seven-day buckets); login
event/session recording; live request byte/endpoint growth. Full suites: 335
offline, 19 e2e.
