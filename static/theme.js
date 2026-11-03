/* Rave Player appearance (web 1.6.35).
 *
 * Dark is the default and lives in app.css :root. The Help dialog writes
 * "dark" | "light" | "system" to localStorage["raveplayer.theme"] and this
 * script resolves it to `data-theme="dark|light"` on <html>. It is loaded as a
 * blocking same-origin script in <head>, before the stylesheet, so the correct
 * theme is on the root element before first paint (no flash) and it needs no
 * inline script or CSP change.
 *
 * "system" is resolved through prefers-color-scheme and kept in sync with the
 * media query; an explicit dark/light choice ignores the OS. The stored value
 * is always the chosen mode, never the resolved one, so a System user follows
 * their device across reloads.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "raveplayer.theme";
  var MODES = ["dark", "light", "system"];
  var DEFAULT_MODE = "dark";
  var DARK_CHROME = "#0a0a0f";
  var LIGHT_CHROME = "#f2eee3";

  var root = document.documentElement;
  var media = null;
  try {
    media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  } catch (_) {
    media = null;
  }

  function normalize(value) {
    var mode = String(value == null ? "" : value).toLowerCase();
    return MODES.indexOf(mode) === -1 ? DEFAULT_MODE : mode;
  }

  function read() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (_) {
      return null;
    }
  }

  function get() {
    return normalize(read());
  }

  function resolve(mode) {
    var chosen = normalize(mode);
    if (chosen === "system") return media && media.matches ? "light" : "dark";
    return chosen;
  }

  function updateMeta(theme) {
    var color = theme === "light" ? LIGHT_CHROME : DARK_CHROME;
    var themeMeta = document.querySelector ? document.querySelector('meta[name="theme-color"]') : null;
    if (themeMeta) themeMeta.setAttribute("content", color);
    var schemeMeta = document.querySelector ? document.querySelector('meta[name="color-scheme"]') : null;
    if (schemeMeta) schemeMeta.setAttribute("content", theme);
  }

  function announce(mode, theme) {
    if (typeof window.dispatchEvent !== "function" || typeof window.CustomEvent !== "function") return;
    try {
      window.dispatchEvent(new window.CustomEvent("rave:theme", {
        detail: { mode: mode, theme: theme },
      }));
    } catch (_) {
      /* Appearance events are advisory; never break the boot. */
    }
  }

  function apply(mode, persist) {
    var chosen = normalize(mode);
    var theme = resolve(chosen);
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-theme-mode", chosen);
    updateMeta(theme);
    if (persist) {
      try {
        window.localStorage.setItem(STORAGE_KEY, chosen);
      } catch (_) {
        /* Private mode / disabled storage: the choice still applies this page. */
      }
    }
    announce(chosen, theme);
    return theme;
  }

  function set(mode) {
    return apply(mode, true);
  }

  function sync() {
    return apply(get(), false);
  }

  function current() {
    return resolve(get());
  }

  // Apply before the stylesheet paints. get() is dark when nothing is stored.
  apply(get(), false);

  if (media) {
    var onSchemeChange = function () {
      if (get() === "system") sync();
    };
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", onSchemeChange);
    } else if (typeof media.addListener === "function") {
      media.addListener(onSchemeChange);
    }
  }

  window.RaveTheme = {
    key: STORAGE_KEY,
    modes: MODES.slice(),
    defaultMode: DEFAULT_MODE,
    normalize: normalize,
    resolve: resolve,
    get: get,
    current: current,
    set: set,
    apply: apply,
    sync: sync,
  };
})();
