# Rave Player web 1.6.80 — release note

Base: `67a991f` (1.6.80). Offline suite: 1043 cases. No Kodi add-on changes.

## 1.6.76 — first-attempt login and mid-stream resume

- **Login reliability.** `admin` is accepted as an alias for the owner identity
  (same hash/role/failure counter). `POST /login` with an already-valid session
  redirects before the CSRF check, so a duplicate submit cannot paint a 400 over
  a good login. Login CSRF rejections record `reason=missing_session|missing|
  mismatch` and re-render with the current token; only real password failures
  count toward the lockout. `GET /login` clears stale auth keys but keeps an
  anonymous CSRF token, and audit-store errors no longer turn a correct login
  into a 500. `static/login.js` is single-shot and reloads a bfcache-restored
  login page; the service worker no longer intercepts `/login` or `/logout`, so
  `Set-Cookie` is always applied (details: `docs/WEB-RELIABILITY-1.6.76.md`,
  `docs/AUTH.md`).
- **Resume on stall.** `/api/stream` reopens the upstream with a Range request
  on a mid-body timeout instead of closing a truncated chunked response, so
  20bensons streams no longer stop after ~1 minute.

## 1.6.77 — signed web playback URLs

`GET /api/webplay?path=…` exchanges the read-only `RAVEWEB_RESOLVER_TOKEN`
bearer for a one-hour HMAC-signed URL accepted by only
`/api/ezgarage/<id>/stream` and `/api/ukraves/<id>/stream`. The permanent token
is still required to mint, the signature is path-scoped and constant-time
verified, and no cookie is needed for playback (details: `docs/WEBPLAY.md`,
`docs/AUTH.md`).

## 1.6.79 — 20bensons stale-while-revalidate + warm

The parsed 20bensons homepage (234 MP3s) is cached for 24 hours. At 70% of the
TTL one deduped daemon thread revalidates it; the stale list is served on a
failed/empty refresh, the first request after a restart warms it once per
process, and `/api/20bensons` never waits for the scrape once a list exists.

## 1.6.80 — service worker merge markers

The `static/sw.js` merge conflict left literal `<<<<<<<`/`=======` markers in
the shipped file; the cache name is now the single
`raveplayer-shell-v1.6.80`, so the worker parses and the old shell cache is
purged on activation.

## Verification

`python -m pytest test -q` → 1043 passed; `test_e2e` → 43 passed. No deploy or
push was performed for the 1.6.80 tag.
