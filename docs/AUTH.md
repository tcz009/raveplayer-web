# Web login (single operator) — 1.6.20

The web app is now closed by default. One operator password unlocks the HTML
shell, every `/api/*` route (including `/api/stream`, `/api/downloads*` and
`/api/surprise`), the download file endpoints and the library static assets.
This is deliberately **one account, one password**: accounts, OAuth and
per-user libraries are deferred.

## Required environment

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | **yes in production** | Signs the session and CSRF cookies. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Production refuses the app if this is empty or a placeholder (`secret`, `change-me`, `dev`, …). Non-production falls back to a random per-process key (sessions die on restart). |
| `RAVEWEB_PASSWORD_HASH` | one of the two | Argon2 (`$argon2id$…`), bcrypt (`$2b$…`) or Werkzeug (`scrypt:`/`pbkdf2:`) password hash. Preferred. |
| `RAVEWEB_PASSWORD` | one of the two | First-run convenience: hashed **in memory** at startup. The generated hash is logged once; copy it into `RAVEWEB_PASSWORD_HASH` and remove the plaintext. Minimum 8 characters. |
| `RAVEWEB_USERNAME` | optional | If set, the login username must match it. Empty/omitted = password-only (username field is optional). |
| `RAVEWEB_ENV` | recommended | Set to `production` on the host. Enables the strict secret-key check. `RAVEWEB_PROD=1` works too; `FLASK_ENV=production` is accepted. |

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
`scrypt`/`pbkdf2` hashes still work (the 1.6.20 test suite exercises this
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

For the current root-owned `/tmp/raveplayer-web.env` deploy pattern, add
`SECRET_KEY=…` and `RAVEWEB_PASSWORD_HASH=…` to that file (mode 600) **before**
restarting the container. The deploy script passes the file with
`--env-file`, and the Dockerfile build check sets a throwaway password only
for `import app`; it never becomes the runtime credential.

## Behaviour

- `GET /login` — designed door page (`templates/login.html`, `static/auth.css`),
  CSRF token in a signed session, password show/hide via external
  `static/login.js`, responsive single column, 44 px controls, focus rings,
  reduced-motion respected.
- `POST /login` — CSRF checked; failures share the generic
  “Invalid username or password.” message; five failures lock that IP for
  15 minutes (`429` + `Retry-After`); success writes
  `raveplayer_session` (`HttpOnly`, `SameSite=Lax`, `Secure` automatically
  when the request is HTTPS) and redirects to a validated same-site `next`.
- `remember me` — sets a persistent cookie and the longer absolute lifetime;
  without it the cookie is a browser-session cookie with a 12 h absolute cap.
- `POST /logout` — `session.clear()` + cookie deletion.
- API requests without a session return `401` JSON
  (`{"error": "authentication required"}`); pages redirect to
  `/login?next=…`; non-login static files return `401`.
- Audit lines: `auth: login success/failure/locked/csrf rejected/logout`, with
  IP and username; never the password or submitted secret.
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
- The failed-login limiter is in-process. The production gunicorn pool is
  `-w 2`, so the effective per-IP limit can be ~2× the configured failures
  across workers; a shared store (Redis) is a later hardening step, not a
  1.6.20 feature. Lockout state resets on restart.
- No accounts/OAuth, no per-user data separation, no password reset flow, no
  email, no 2FA, no server-side session store/revocation list (signed cookies
  only, revoked by rotating `SECRET_KEY`). The existing global favourites/
  Downloads library remains shared by all logged-in clients (single operator).

## Tests

- `test/test_auth.py` (15): unauth API JSON 401s, page redirects, narrow public
  surface, designed login + CSRF, generic failure, rate limit/lockout, cookie
  flags (HttpOnly/SameSite/Secure over HTTPS), remember window, expiry, logout,
  open-redirect sanitation, username enforcement, config fail-closed,
  first-run hashing.
- e2e `test_login_logout_flow_and_login_page_design`: logout → door page →
  password reveal → generic error → remember-me login → app; screenshots under
  `rave-player-space/reports/web-1.6.20/` when `RAVEWEB_ARTIFACTS_DIR` is set.
