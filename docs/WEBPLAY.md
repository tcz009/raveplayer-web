# Signed playback URLs for web-proxied Mediafire rows

Status: web-only `/api/webplay` plus a signed-token branch in the auth guard.
No Kodi add-on files are changed and nothing is deployed.

Files:

- `core/webplay.py` — path allowlist, HMAC-SHA256 signing, constant-time verify.
- `app.py` — `GET /api/webplay`, service-token allowlist entry, auth-guard
  branch for the two stream paths.
- `test/test_webplay.py` — offline tests.

## 1. Why this exists

The EZ Garage and UK Raves rooms play Mediafire files through the web app's
own stream proxy:

```text
/api/ezgarage/<id>/stream
/api/ukraves/<id>/stream
/api/ezgarage/<id>/file
/api/ukraves/<id>/file
```

Those routes are session-only, exactly like every other `/api/*` route.  A
browser sends the login cookie automatically for an `<audio>` element.  Kodi
cannot: it has no browser cookie, and the URL handed to its player cannot carry
the `RAVEWEB_RESOLVER_TOKEN` bearer header.  The rows therefore answer `401`.

`/api/webplay` exchanges the narrow service bearer for a short-lived signed URL
that the two **stream** routes accept without a cookie.  The permanent service
token is still required to mint; the playback URL is only a time-boxed
capability for one path.

## 2. Endpoint contract

```http
GET  /api/webplay?path=%2Fapi%2Fezgarage%2F72aswcrx4hlw2sh%2Fstream
HEAD /api/webplay?path=...
Authorization: Bearer <RAVEWEB_RESOLVER_TOKEN>
```

`path` must be a **same-origin relative path** matching exactly:

```regex
^/api/(ezgarage|ukraves)/[A-Za-z0-9_-]+/stream$
```

Absolute URLs, protocol-relative URLs, query strings, fragments, trailing
slashes, other sources, `/file` children and every other `/api/*` route are
rejected with `400 {"error": "...", "code": "invalid_path"}`.  Missing or wrong
bearer → `401`.  `RAVEWEB_RESOLVER_TOKEN` unset → `401`.  Only `GET`/`HEAD` are
accepted; other methods return `405` with `Allow: GET, HEAD`.

Success (`200`):

```json
{
  "ok": true,
  "url": "http://rave.local:8080/api/ezgarage/72aswcrx4hlw2sh/stream?e=1794220000&t=4f9c..."
}
```

`url` is absolute (built from the request root, like `/api/mega/sign`).  The
query is exactly `e=<unix expiry>` then `t=<lowercase hex>`.

### Signature

```text
t = HMAC-SHA256(SECRET_KEY, path + "\0" + expiry)
```

- `SECRET_KEY` is the app's Flask signing key (`app.secret_key`, exported as
  `SECRET_KEY` in production).  Rotating it invalidates every outstanding
  signed playback URL (and every login session), which is the intended
  revocation mechanism.
- Both the path bytes and the expiry spelling are signed.  The signature is
  bound to the exact request path, so it cannot be replayed at another id,
  the other source, a `/file` route, `/api/webplay`, `/api/mega/stream` or any
  other endpoint.
- Default TTL is **1 hour** (`TOKEN_TTL = 3600`).  Expired and far-future
  expiries are rejected.  Verification uses `hmac.compare_digest`.
- Tokens are stateless: there is no per-token revocation.  Rotate `SECRET_KEY`
  to invalidate all of them.

### Playing

The returned URL needs no headers and no cookie:

```sh
curl -sS -H "Authorization: Bearer $RAVEWEB_RESOLVER_TOKEN" \
  "http://rave.local:8080/api/webplay?path=$(python -c \
    'import urllib.parse; print(urllib.parse.quote("/api/ukraves/7524/stream", safe=""))')"
# {"ok":true,"url":"http://rave.local:8080/api/ukraves/7524/stream?e=...&t=..."}
```

In Kodi, feed the `url` value to `setResolvedUrl`/`PlayMedia` as an ordinary
HTTP URL.  `Range` requests work on the stream routes; the signature stays
valid until `e`.

```xml
<!-- play the returned absolute URL directly; add no Referer/User-Agent -->
<item><link>http://rave.local:8080/api/ukraves/7524/stream?e=...&amp;t=...</link></item>
```

## 3. Auth guard

`_auth_guard` checks, before the session fallback:

1. `/api/mega/stream` with a valid MEGA token (existing behaviour);
2. **`GET`/`HEAD` on a path that is itself signable**, with a valid `e`/`t`
   for that exact path;
3. `/api/webplay` only through `_resolver_token_ok(require_bearer=True)`.

A valid signature never opens any other path.  A session cookie still works on
the stream routes exactly as before, and an invalid/expired/cross-path
signature with no session returns `401`.  The signed query is deliberately not
added to `RESOLVER_TOKEN_GET_PATHS`: `/api/webplay` is the only new
service-token route, and the stream paths remain closed to the raw bearer.

### Logging

The app's request line and audit row use `request.path` only — never the query
string — so `t` is not written to application logs or the audit database.  If a
reverse proxy in front of the app logs full request lines including query
strings, add a query-redaction rule for `/api/(ezgarage|ukraves)/.*/stream` (or
disable query logging for those paths).  Do not paste a signed URL into a
ticket or chat; it is a bearer capability until `e`.

## 4. Tests

`test/test_webplay.py` covers:

- signature format (`HMAC-SHA256(path + NUL + expiry)`), one-hour default,
  exact path allowlist and rejection of absolute/other/traversal paths;
- bearer-only `/api/webplay`, wrong/missing token, session-only client, GET and
  HEAD, 405 for POST/PUT/PATCH/DELETE;
- signed EZ and UK paths stream through a fake upstream with no cookie;
- tampered token, bumped expiry, leading-zero expiry, expired signature,
  cross-path/cross-id replay and missing `e`/`t` → `401`, with no upstream
  open;
- signatures replayed at `/api/latest`, `/file` children, `/api/mega/stream`,
  `/api/resolve` and `/api/webplay` → `401`;
- app logs never contain the signature.
