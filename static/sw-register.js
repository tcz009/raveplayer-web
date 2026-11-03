/* Register the app-shell service worker on secure origins only.
 *
 * iOS refuses service workers on plain http:// LAN addresses; the manifest
 * (`display: standalone`, `start_url: "/"`) plus the apple-mobile-web-app meta
 * tags are what make "Add to Home Screen" launch standalone there.  HTTPS
 * deployments (reverse proxy, tunnel, localhost) also get offline shell
 * navigation and static caching.
 *
 * 1.6.31: report registration/worker state to the opt-in debug log. The guard
 * keeps this a no-op (one property read) when the log is off.
 */
(function () {
  if (!("serviceWorker" in navigator)) return;
  if (!window.isSecureContext) return;

  function announce(state, extra) {
    if (!(window.RaveDebug && window.RaveDebug.enabled)) return;
    try {
      document.dispatchEvent(new CustomEvent("rave:sw", {
        detail: Object.assign({ state: state }, extra || {}),
      }));
    } catch (_) {
      /* Reporting must never break registration. */
    }
  }

  function track(worker, label) {
    if (!worker) return;
    announce(label, { worker: worker.state });
    worker.addEventListener("statechange", function () {
      announce("statechange", { worker: worker.state });
    });
  }

  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (registration) {
      announce("registered", { scope: registration.scope });
      registration.addEventListener("updatefound", function () {
        announce("updatefound");
      });
      track(registration.installing, "installing");
      track(registration.waiting, "waiting");
      track(registration.active, "active");
    }).catch(function () {
      announce("error");
      /* A failed registration must never block the app. */
    });
  });
})();
