# Playback presence + owner history — 1.6.34

Opt-in-free, owner-only visibility of what the two local identities are
playing. The browser sends a **best-effort** heartbeat; playback never waits on
it and failures are ignored. Only labels, source keys, positions and times are
stored — **never media URLs, query strings, cookies or tokens**.

## Heartbeat

`POST /api/playback` (any signed-in owner/friends session)

```json
{"state":"playing","label":"Dreamscape 10","source":"archive",
 "position":42.5,"duration":3600,"client_ts":1793690000.2}
```

- `state`: `playing` | `paused` | `stopped`; `label` ≤300, `source` ≤64,
  `position`/`duration` finite and 0–86400 s, `client_ts` optional numeric.
- Body ≤4 KiB; invalid bodies → `400`, too many heartbeats → `429`.
- Rate limit **60 requests/minute per session**. Success is `204` with no body.
- Stored per **identity + session** in the auth SQLite DB (`playback_now`,
  `playback_events`, WAL). A state/label/source change also writes an audit
  `play_event` row (state + source only).
- A heartbeat older than **90 seconds** is presented as `stopped`/offline.

Client: sends on play, pause, stop/close, seek and every 30 s while playing;
`pagehide`/`beforeunload` send a keepalive `stopped`. Kodi playback is
unaffected (web client only).

## Owner endpoints (friends 403; widget token 401)

`GET /api/admin/playback` →

```json
{"stale_after_seconds":90,
 "now":[{"identity":"owner","session_id_masked":"3f2a9c1d8e…",
         "state":"playing","label":"Dreamscape 10","source":"archive",
         "position":42.5,"duration":3600,
         "updated":"2026-11-03T12:30:00Z","age_seconds":3.2,"online":true}]}
```

One row per identity (latest session), both identities for the owner; masked
session ids only, no URL/token fields, no `online` state for a stale row.

`GET /api/admin/playback/history?hours=24&identity=owner` →

```json
{"hours":24,"identity":"owner",
 "items":[{"at":"2026-11-03T12:30:00Z","identity":"owner",
           "session_id_masked":"3f2a9c1d8e…","event":"play_event",
           "state":"playing","label":"Dreamscape 10","source":"archive",
           "position":42.5,"duration":3600}]}
```

`hours` is clamped to 1–168 (invalid → 400); `identity` is optional
`owner`/`friends`. History is newest-first, capped at 500 rows.

`GET /api/admin/playback/export?hours=24` → JSON attachment
(`Content-Disposition: attachment; filename="rave-playback-YYYYMMDD-HHMMSS.json"`,
`Content-Type: application/json`, `Cache-Control: no-store`):

```json
{"kind":"rave-playback-log","exported_at":"…","hours":24,
 "stale_after_seconds":90,"now":[…],"history":[…]}
```

## Retention/configuration

| Variable | Default | Purpose |
|---|---|---|
| `RAVEWEB_PLAYBACK_RETENTION_DAYS` | `7` | Drop playback events older than this. |
| `RAVEWEB_PLAYBACK_EVENT_LIMIT` | `5000` | Hard cap on stored playback events (oldest removed first). |

Tables live in the existing auth DB (`RAVEWEB_AUTH_DB`), so the data volume
backup covers them. Playback rows are only readable through owner/admin routes.

## Privacy

This is presence telemetry for the owner's own LAN app, not analytics. It is
best-effort, has no third-party destination, keeps no URL/token/media bytes, is
masked by session and retained for 7 days by default. Friends can neither read
the playback views nor the export.

## Tests

`test/test_playback.py` (6): auth/validation/rate limit, owner sees both
identities, friends 403 on all reads, stale→stopped, history window/filter and
`play_event` audit rows, export shape/headers with no URLs/tokens. Records are
stored in the auth SQLite DB; client heartbeats are best-effort by design.
