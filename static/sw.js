/* Rave Player service worker (1.6.57).
 *
 * Purpose: the home-screen app must always boot the local app shell, never a
 * stale Safari tab or a page from another origin.  The worker therefore:
 *   - only ever handles GET requests for its OWN origin,
 *   - handles navigations network-first and falls back to the cached "/" shell
 *     (so any same-origin deep link renders the app),
 *   - caches versioned static assets at fetch time (the ?v= query means a
 *     deploy never serves yesterday's JS from the cache),
 *   - never caches /api/* or /api/downloads/* (live data/local library),
 *   - never caches a redirected /login response as the "/" shell (a signed-out
 *     fetch of "/" follows the 302 to /login; caching that HTML under "/" made
 *     the first post-login navigation show the login page again),
 *   - ignores cross-origin requests entirely (ads/third-party pages).
 */
<<<<<<< HEAD
const CACHE_NAME = "raveplayer-shell-v1.6.79";
=======
const CACHE_NAME = "raveplayer-shell-v1.6.79";
>>>>>>> fix/web-reliability
const SHELL = "/";

function shellResponse(response) {
  // Only a direct 200 for "/" is the app shell.  A 302 to /login followed by
  // fetch() yields a 200 document at /login (response.redirected true); that
  // must never become the cached shell.
  if (!response || !response.ok || response.redirected) return false;
  try {
    const url = new URL(response.url);
    return url.origin === self.location.origin && url.pathname === SHELL;
  } catch (_) {
    return false;
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      const response = await fetch(new Request(SHELL, {
        cache: "reload", redirect: "manual",
      }));
      if (shellResponse(response)) await cache.put(SHELL, response);
    } catch (_) {}
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key !== CACHE_NAME)
      .map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  let url;
  try { url = new URL(request.url); } catch (_) { return; }
  if (url.origin !== self.location.origin) return;        // ads / third parties
  if (url.pathname === "/sw.js") return;
  if (url.pathname.startsWith("/api/")) return;           // live data only

  if (request.mode === "navigate") {
    // The app itself is hash-routed, so "/" is the only navigation that can be
    // answered from the cached shell.  Real server pages that set cookies
    // (/login, /logout) must be fetched by the browser itself: a response
    // returned from respondWith does not apply its Set-Cookie, so intercepting
    // /login left the rendered CSRF token without the matching session cookie
    // (the first POST was then rejected as "session expired").
    if (url.pathname !== SHELL) {
      if (url.pathname === "/login" || url.pathname === "/logout"
          || url.pathname.indexOf("/login/") === 0) {
        return;
      }
      event.respondWith((async () => {
        try {
          const response = await fetch(request);
          if (response && response.ok) return response;
        } catch (_) { /* offline or blocked: fall through to the shell */ }
        const cached = await caches.match(SHELL, { ignoreSearch: true });
        return cached || Response.error();
      })());
      return;
    }
    // Stale-while-revalidate: a warm launch paints the cached shell
    // immediately while the network copy refreshes in the background, so the
    // home-screen app never waits a round trip to show the UI.  A cold launch
    // (no shell yet) still waits for the network and falls back to the cache
    // when it fails.
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(SHELL, { ignoreSearch: true });
      const network = fetch(request).then(async (response) => {
        if (shellResponse(response)) {
          try { await cache.put(SHELL, response.clone()); } catch (_) { /* quota */ }
        }
        return response && response.ok ? response : null;
      }).catch(() => null);
      if (cached) {
        event.waitUntil(network);
        return cached;
      }
      const response = await network;
      return response || Response.error();
    })());
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request);
      if (cached) {
        // Versioned URLs make stale entries harmless; refresh in the background.
        event.waitUntil(fetch(request).then((response) => {
          if (response && response.ok) cache.put(request, response);
        }).catch(() => {}));
        return cached;
      }
      try {
        const response = await fetch(request);
        if (response && response.ok) cache.put(request, response.clone()).catch(() => {});
        return response;
      } catch (_) {
        return Response.error();
      }
    })());
  }
});
