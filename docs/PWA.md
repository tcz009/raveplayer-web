# Home-screen / PWA install (web 1.6.19)

## Bug

On iPhone the home-screen icon sometimes opened an unrelated ad page
(`bike-components.de`) instead of Rave Player, especially on resume or on a
second tap.

## Root cause

The manifest already had `start_url: "/"` and `display: "standalone"`, but the
template had **no iOS `apple-mobile-web-app-capable` meta tag**, no stable
manifest `id`, and the app had **no service worker**. On iOS Safari that means
"Add to Home Screen" falls back to saving the *current Safari tab* as a web
clip/bookmark rather than installing the manifest's start URL as a standalone
app. If the tab had been redirected to an ad when the icon was created (or
Safari restored that stale tab), the icon opened the ad. Without a worker there
was also no same-origin app-shell fallback, so an old/foreign URL inside the
tab was never normalised back to the app.

## Fix (1.6.19)

- `templates/index.html`: `apple-mobile-web-app-capable=yes`,
  `mobile-web-app-capable=yes`, `apple-mobile-web-app-title`,
  `apple-mobile-web-app-status-bar-style`, plus the existing
  `apple-touch-icon`/`manifest`/`theme-color`.
- `/manifest.webmanifest`: explicit **`id: "/"`**, **`start_url: "/"`**,
  **`scope: "/"`**, `display: standalone` (+ `display_override`), 180/512 icons,
  `lang`, `no-cache` so iOS re-reads it.
- `/sw.js` + `static/sw.js` served at the **root** with
  `Service-Worker-Allowed: /`:
  - handles only GET, same-origin requests (cross-origin/ads are never touched),
  - navigations are network-first and fall back to the cached `/` shell, so
    *any* same-origin link boots the app,
  - `/api/*` and the local Downloads library are never cached,
  - static assets are cached by their full `?v=<version>` URL, so a deploy
    cannot serve yesterday's JS.
- `static/sw-register.js` registers the worker **only in a secure context**
  (HTTPS or localhost). CSP is unchanged (`script-src 'self'`); no inline JS.

## Re-add the home-screen icon (required once)

The old icon is a bookmark, not the new app; remove and re-add it:

1. Long-press the Rave Player icon → **Remove App** / **Delete Bookmark**.
2. In Safari open the app **root** (e.g. `http://<server>:8097/`) and wait for
   Latest to render — do not add from an ad/redirected page.
3. Share → **Add to Home Screen**. The sheet should show “Rave Player” and the
   URL should be the site root, not a deep/ad URL.
4. Launch once from the new icon; it should open standalone (no Safari chrome).

Deleting website data (Settings → Safari → Advanced → Website Data → Rave
Player) will also drop the old bookmark/caches if the icon still misbehaves.

## iPhone over plain HTTP (LAN) — limitation

iOS only allows service workers on secure origins. The live box is currently
plain `http://192.168.1.179:8097`, so on the phone the **manifest + apple meta
tags still give a standalone app that starts at `/`**, but the worker will not
register (the registration is skipped without an error). The app remains
usable; only the offline shell/navigation fallback is inactive. For the full
PWA behaviour serve the app over HTTPS (reverse proxy with a certificate, or a
tunnel) — then the same code registers the worker automatically.

## Tests

- `test/test_pwa.py` (5): manifest identity/icons/no-cache, apple meta tags,
  root-scoped `/sw.js` headers, worker same-origin/API/cross-origin rules and
  shell fallback, secure-context registration.
- e2e: `test_pwa_installs_and_any_same_origin_link_opens_the_app` — reads the
  manifest, waits for a root-scoped worker, reloads to become controlled, then
  opens `/stale-ad-or-any-link` and asserts the app shell renders while
  `/api/menu` still goes to the network.

## Cockney Player

The separate Cockney Player app needs the same fix: `id`/`start_url: "/"`/
`scope: "/"`/standalone manifest, the apple-mobile-web-app meta tags, a
root-scoped service worker with a same-origin network-first navigation
fallback that never caches API/downloads/cross-origin requests, secure-context
registration, and the same remove-and-re-add step for existing home-screen
icons. (No code changes made here — separate repo.)
