# Owner write API — 1.6.25

Phase 2 of the Control Centre integration. **Owner/admin session only**
(`require_admin`); friends/viewer sessions get **403**, and a widget bearer token
cannot call any write route. Every successful/failed action is written to the
auth audit store (`events` with `event LIKE 'admin_%'`) and surfaces in
`/api/admin/stats.recent_actions`.

## CSRF and rate limiting

- `GET /api/admin/csrf` (owner only) → `{"csrf":"…"}`. Tokens are the signed
  session's CSRF value; fetch once per page/session.
- Every `POST`/`DELETE` below requires header **`X-CSRF-Token: <token>`** (or a
  `csrf` field in a form/JSON body). Missing/bad token → **403**
  `{"error":"csrf token required"}` and an `admin_csrf_rejected` audit row.
- Admin writes are rate-limited per IP: **30 actions/minute**, then
  **429** `{"error":"too many admin actions"}` with `Retry-After`; an
  `admin_rate_limited` audit row is written.
- Responses are JSON, `no-store`, and never contain session tokens, password
  hashes or other secrets.

## Sessions (masked ids only)

`GET /api/admin/sessions` (read) →

```json
{"sessions":[
  {"session_id_masked":"3f2a9c1d8e…","identity":"owner","role":"admin",
   "ip":"192.168.1.10","created":1793560000.0,
   "last_seen":1793561200.0,"expires_at":1793600000.0}
]}
```

`POST /api/admin/sessions/<masked_id>/revoke` (CSRF) →

```json
{"revoked":true,"session_id_masked":"3f2a9c1d8e…","identity":"friends","role":"viewer"}
```

The route accepts only the **masked** value (URL-encode `…`); raw session ids
return 404. Revoking the caller's own session is allowed; the next request fails
auth. Audit event `admin_session_revoked`.

## Blocks

- `GET /api/admin/blocks` → `{"blocks":[{id,kind,value,reason,created_at,until,created_by}]}`
  (active only).
- `POST /api/admin/blocks` (CSRF) body:
  `{"kind":"ip"|"identity","value":"…","reason":"…","ttl_seconds":600}`
  (`ttl_seconds` optional; default 3600, clamped 60 s–30 days) → **201**
  `{"block":{…}}`.
  Existing sessions for that identity/IP are revoked; the auth guard also
  rejects them on the next request. Login for a blocked identity/IP returns the
  generic **403 “Access temporarily blocked.”** (no hint which block matched).
- `DELETE /api/admin/blocks/<id>` (CSRF) → `{"deleted":true,"id":…}`; audit
  `admin_block_deleted`.
- Audit events: `admin_block_created`, `admin_block_deleted`.

> Blocking your own IP/identity locks the owner out too. Confirm the target from
> the sessions list before adding an `ip` block, and keep a second admin path
> (container exec) for emergency unblock. Expired blocks stop applying
> automatically.

## Friends login

- `GET /api/admin/friends` → `{"friends_enabled":true,"disabled":false}`.
- `POST /api/admin/friends/disable` (CSRF) → `{"friends_enabled":false}`. Sets a
  persisted runtime flag in the auth DB, revokes every `friends` session, and
  makes friends login fail generically until re-enabled. Audit
  `admin_friends_disabled`.
- `POST /api/admin/friends/enable` (CSRF) → `{"friends_enabled":true}` (subject
  to `RAVEWEB_FRIENDS_PASSWORD_HASH` being configured). Audit
  `admin_friends_enabled`.
- No secret is returned or logged. Hash rotation remains a host-env action in
  this release (change `RAVEWEB_FRIENDS_PASSWORD_HASH` and restart); the
  one-time-return rotation flow is deferred.

## Stats additions

`GET /api/admin/stats` now also returns:

```json
{
  "blocked_now": 1,
  "recent_actions": [
    {"at":"2026-11-01T20:20:00Z","event":"admin_block_created",
     "identity":"owner","role":"admin","ip":"192.168.1.10",
     "endpoint":"/api/admin/blocks","status":201,"detail":"identity:friends"}
  ]
}
```

`recent_actions` is the newest 12 `admin_*` events (masked/target details only;
never session tokens or password material). `blocked_now` counts unexpired
blocks.

## Ownership / auth rules

| Caller | Read `/api/admin/sessions|blocks|friends|csrf` | Write routes |
|---|---|---|---|
| owner/admin session | 200 | 200/201/204 with CSRF |
| friends/viewer session | 403 | 403 |
| widget bearer token only | 401 | 401 |
| no session | 401 | 401 |

Runtime data lives in the auth SQLite store (`RAVEWEB_AUTH_DB`, default
`$RAVEWEB_DATA/auth.sqlite3`): `sessions`, `blocks`, `settings`, `events`.
`login_failures` remains the shared login lockout table.

## Tests

`test/test_admin_write.py` (7): owner session list/revoke (masked only), CSRF
rejection, friend 403 on every admin surface, identity/IP block denying login
and existing sessions, block list/delete, runtime friends disable/enable,
rate-limit 429, and audit recent-action rows.
