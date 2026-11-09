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
 *   - ignores cross-origin requests entirely (ads/third-party pages).
 */
const CACHE_NAME = "raveplayer-shell-v1.6.58";
const SHELL = "/";

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    try { await cache.add(new Request(SHELL, { cache: "reload" })); } catch (_) {}
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
    // answered from the cached shell.  Every other path is a real server page
    // (/login, /logout) and must be fetched: serving the shell there would show
    // the app to a signed-out user.
    if (url.pathname !== SHELL) {
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
        if (response && response.ok) {
          try { await cache.put(SHELL, response.clone()); } catch (_) { /* quota */ }
          return response;
        }
        return null;
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
