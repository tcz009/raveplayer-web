/* Register the app-shell service worker on secure origins only.
 *
 * iOS refuses service workers on plain http:// LAN addresses; the manifest
 * (`display: standalone`, `start_url: "/"`) plus the apple-mobile-web-app meta
 * tags are what make "Add to Home Screen" launch standalone there.  HTTPS
 * deployments (reverse proxy, tunnel, localhost) also get offline shell
 * navigation and static caching.
 */
(function () {
  if (!("serviceWorker" in navigator)) return;
  if (!window.isSecureContext) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {
      /* A failed registration must never block the app. */
    });
  });
})();
