# Web login (owner + friends) — 1.6.21

The web app is now closed by default. Owner or friends logins unlock the HTML
shell, every `/api/*` route (including `/api/stream`, `/api/downloads*` and
`/api/surprise`), the download file endpoints and the library static assets.
There are exactly two fixed identities: **owner** (role `admin`) and
**friends** (role `viewer`). They share the full player; owner-only routes are
gated by the reusable `require_admin` decorator (example: `/api/admin/whoami`).
Arbitrary accounts, OAuth and per-user libraries are deferred.
A read-only owner stats feed (`GET /api/admin/stats`) is documented in
[`docs/ADMIN.md`](ADMIN.md); it accepts an owner session or a read-only
`RAVEWEB_WIDGET_TOKEN` bearer.

## Required environment

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | **yes in production** | Signs the session and CSRF cookies. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Production refuses the app if this is empty or a placeholder (`secret`, `change-me`, `dev`, …). Non-production falls back to a random per-process key (sessions die on restart). |
| `RAVEWEB_PASSWORD_HASH` | **yes** | Owner / admin password hash: Argon2 (`$argon2id$…`), bcrypt (`$2b$…`) or Werkzeug (`scrypt:`/`pbkdf2:`). Username `owner`. |
| `RAVEWEB_FRIENDS_PASSWORD_HASH` | recommended | Friends / viewer password hash. If omitted, only owner can sign in (friends login stays closed). Rotating just this value changes the friends password without touching owner. |
| `RAVEWEB_PASSWORD` | first-run alternative | Owner plaintext hashed **in memory** at startup; generated hash logged once for `RAVEWEB_PASSWORD_HASH`. |
| `RAVEWEB_FRIENDS_PASSWORD` | first-run alternative | Friends plaintext hashed in memory; generated hash logged once for `RAVEWEB_FRIENDS_PASSWORD_HASH`. |
| `RAVEWEB_ENV` | recommended | Set to `production` on the host. Enables the strict secret-key check. `RAVEWEB_PROD=1` works too; `FLASK_ENV=production` is accepted. |
| `RAVEWEB_WIDGET_TOKEN` | optional | Bearer token for the read-only `GET /api/admin/stats` Control Centre feed; empty disables bearer access. |
| `RAVEWEB_AUTH_DB` | `$RAVEWEB_DATA/auth.sqlite3` | Persistent SQLite audit/session store (login events, sessions, stats, shared login-failure counters). |

Optional tuning:

| Variable | Default | Purpose |
|---|---|---|
| `RAVEWEB_SESSION_HOURS` | `12` | Absolute session lifetime without “remember me”. |
| `RAVEWEB_REMEMBER_DAYS` | `30` | Absolute lifetime with “remember me”. |
| `RAVEWEB_LOGIN_MAX_FAILURES` | `5` | Failed attempts before a lockout (per IP, per worker). |
| `RAVEWEB_LOGIN_FAILURE_WINDOW` | `600` | Failure window in seconds. |
| `RAVEWEB_LOGIN_LOCKOUT` | `900` | Lockout duration in seconds. |

The app **fails closed**: if neither hash nor plaintext is set, it refuses to
start. `requirements.txt` includes `argon2-cffi` and `bcrypt`; the Docker image
build installs both. In a bare local venv without them, Werkzeug
`scrypt`/`pbkdf2` hashes still work (the 1.6.21 test suite exercises this
fallback).

## Generate a hash

```sh
# in the app environment (Argon2 preferred when argon2-cffi is installed)
python -c "from core.auth import generate_hash; print(generate_hash('your password'))"

# or try existing sources
python -c "from core.auth import validate_scheme; validate_scheme('$...')"
```

Then export on the host (never commit it):

```sh
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export RAVEWEB_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$…'
export RAVEWEB_ENV=production
```

`scripts/deploy.sh` now refuses to run unless `SECRET_KEY`,
`RAVEWEB_PASSWORD_HASH` and `RAVEWEB_FRIENDS_PASSWORD_HASH` are exported; it
copies them to the host's root-only `/tmp/raveplayer-web-auth.env`, merges that
into the container `--env-file`, and deletes both on every path. `RAVEWEB_ENV`
defaults to `production` for the deploy. Set them in your shell (or a
root-only env file you source), never in the repo. The Dockerfile build check
sets a throwaway password only for `import app`; it never becomes the runtime
credential.

## Behaviour

- `GET /login` — designed door page (`templates/login.html`, `static/auth.css`),
  CSRF token in a signed session, password show/hide via external
  `static/login.js`, responsive single column, 44 px controls, focus rings,
  reduced-motion respected. The username field hints generically at
  `owner or friends` but does not reveal which identities are configured.
- `POST /login` — CSRF checked; username selects `owner` (admin) or `friends`
  (viewer). Both pairs share the generic “Invalid username or password.”
  message; each **identity + IP** has its own 5-failure / 15-minute lockout, so
  a locked-out friends login does not lock the owner (and vice versa). Success
  writes the role into `raveplayer_session` (`HttpOnly`, `SameSite=Lax`,
  `Secure` automatically when HTTPS) and redirects to a validated same-site
  `next`.
- `remember me` — sets a persistent cookie and the longer absolute lifetime;
  without it the cookie is a browser-session cookie with a 12 h absolute cap.
- `POST /logout` — `session.clear()` + cookie deletion.
- API requests without a session return `401` JSON
  (`{"error": "authentication required"}`); pages redirect to
  `/login?next=…`; non-login static files return `401`.
- Audit lines: `auth: login success/failure/locked/csrf rejected/logout`,
  with IP, identity and role; never the password or submitted secret.
- HTTPS responses add `Strict-Transport-Security`; all responses keep
  `X-Content-Type-Options: nosniff` and `X-Frame-Options: SAMEORIGIN`; the
  shell/login CSP is unchanged (`script-src 'self'`, `style-src 'self'`).
- Public without login: `/login`, `/logout`, `/healthz` (for Docker health),
  `/manifest.webmanifest`, `/sw.js`, the PWA register script, icons and the
  login-only `auth.css`/`login.js`. Everything else, including app CSS/JS/art,
  requires the session.

## Host / proxy setup

- Put the app on your LAN/Tailscale only; do not publish the port.
- Terminate HTTPS at a reverse proxy and forward `X-Forwarded-Proto: https`
  (or run gunicorn behind TLS itself). The app marks the session cookie
  `Secure` and emits HSTS on HTTPS requests; it does **not** force an HTTP→HTTPS
  redirect, so keep the proxy redirect in place.
- Do not add a secret to a tracked file, Dockerfile, compose file, README or
  shell history. Keep the env file root-owned and mode 600.
- A stolen session cookie is valid until logout/expiry; changing `SECRET_KEY`
  invalidates **all** sessions immediately.
- The failed-login limiter is keyed by **identity + IP** and lives in the shared
  SQLite audit store, so both gunicorn workers enforce one policy (no ~2×
  loophole). Lockout state survives a worker restart and clears on success or
  after the failure window.
- Only the two fixed identities; no arbitrary accounts/OAuth, no password
  reset flow, no email, no 2FA, no server-side session store/revocation list
  (signed cookies only, revoked by rotating `SECRET_KEY`). Favourites and Downloads are scoped per identity since 1.6.26 (the legacy global
  store becomes the owner's); see [`PER-USER.md`](PER-USER.md).

## Tests

- `test/test_auth.py` (18): unauth API JSON 401s, page redirects, narrow public
  surface, designed login + CSRF, generic failure, rate limit/lockout, cookie
  flags (HttpOnly/SameSite/Secure over HTTPS), remember window, expiry, logout,
  open-redirect sanitation, owner/friends role separation, admin-probe denial,
  independent per-identity lockouts and friends hash rotation, config
  fail-closed, first-run hashing.
- e2e `test_login_logout_flow_and_login_page_design`: logout → door page →
  password reveal → generic error → remember-me login → app; screenshots under
  `rave-player-space/reports/web-1.6.21/` when `RAVEWEB_ARTIFACTS_DIR` is set.
