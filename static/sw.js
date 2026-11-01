/* Rave Player service worker (1.6.19).
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
const CACHE_NAME = "raveplayer-shell-v1.6.19";
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
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response && response.ok) {
          const cache = await caches.open(CACHE_NAME);
          cache.put(SHELL, response.clone()).catch(() => {});
          return response;
        }
      } catch (_) { /* offline or blocked: fall through to the shell */ }
      const cached = await caches.match(SHELL, { ignoreSearch: true });
      return cached || Response.error();
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
