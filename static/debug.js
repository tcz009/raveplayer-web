/* Rave Player — opt-in client-side debug log (1.6.32).
 *
 * Off by default: when the flag is absent this file reads the flag, binds the
 * Help switch and stops. No panel, no fetch wrapper, no capture listeners and
 * no storage writes until it is switched on.
 *
 * Enable with `?debug=1` (persisted in localStorage), the Help dialog's
 * "Debug log: off" switch, or `localStorage["raveplayer.debug"] = "1"` and
 * reload. `?debug=0` and the switch's off state clear it.
 * The panel is local only: Copy goes to the clipboard and Download JSON writes
 * a file, never the network. Every value is redacted before it is stored.
 *
 * Exported for the Node unit tests (`test/js/debug_spec.js`); in the browser it
 * installs itself as `window.RaveDebug`.
 */
(function (root) {
  "use strict";

  const STORAGE_KEY = "raveplayer.debug";
  const MAX_EVENTS = 500;
  const MAX_RENDERED = 200;
  const MAX_STRING = 300;

  // Cookie/token/password/session material never reaches the buffer.
  const SENSITIVE_KEY = /pass(word|wd)?|token|secret|cookie|authoriz|session|csrf|api[-_]?key|signature|credential|^sid$/i;
  const SENSITIVE_PARAM = /\b(password|passwd|pass|token|access_token|refresh_token|secret|cookie|session|sessionid|sid|csrf|api[_-]?key|key|auth|authorization|signature)=([^&#\s;]+)/gi;
  const SESSION_COOKIE = /\braveplayer_session\s*=\s*[^;\s&]+/gi;
  const BEARER = /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi;
  const JWT = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;

  function isBrowser() {
    return typeof window !== "undefined" && typeof document !== "undefined";
  }

  /* ------------------------------------------------------------ toggle */

  function flagFromSearch(search) {
    const match = /[?&]debug=(1|0|true|false)\b/i.exec(search || "");
    if (!match) return null;
    return match[1] === "1" || match[1].toLowerCase() === "true";
  }

  // Explicit query string wins over the stored flag; anything else is off.
  function resolveEnabled(search, stored) {
    const flag = flagFromSearch(search);
    if (flag !== null) return flag;
    return stored === "1" || stored === "true";
  }

  function storageGet() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (_) {
      return null;
    }
  }

  function storageSet(value) {
    try {
      if (value === null) window.localStorage.removeItem(STORAGE_KEY);
      else window.localStorage.setItem(STORAGE_KEY, value);
    } catch (_) {
      /* Private mode/quota: the query-string switch still works this load. */
    }
  }

  /* ------------------------------------------------------------ redaction */

  function redactString(value) {
    let text = String(value);
    text = text.replace(SENSITIVE_PARAM, "$1=[redacted]");
    text = text.replace(SESSION_COOKIE, "raveplayer_session=[redacted]");
    text = text.replace(BEARER, "Bearer [redacted]");
    text = text.replace(JWT, "[redacted-jwt]");
    if (text.length > MAX_STRING) text = text.slice(0, MAX_STRING) + "…";
    return text;
  }

  function redact(value, key, seen, depth) {
    depth = depth || 0;
    if (key && SENSITIVE_KEY.test(key)) return "[redacted]";
    if (value === null || value === undefined) return value;
    if (typeof value === "string") return redactString(value);
    if (typeof value === "number" || typeof value === "boolean") return value;
    if (typeof value === "function") return "[function]";
    if (depth > 6) return "[depth]";
    seen = seen || [];
    if (seen.indexOf(value) >= 0) return "[circular]";
    seen.push(value);
    let out;
    if (Array.isArray(value)) {
      out = value.slice(0, 50).map((entry) => redact(entry, "", seen, depth + 1));
    } else {
      out = {};
      Object.keys(value).slice(0, 30).forEach((name) => {
        out[name] = redact(value[name], name, seen, depth + 1);
      });
    }
    seen.pop();
    return out;
  }

  /* ------------------------------------------------------------ buffer */

  const state = {
    enabled: false,
    started: false,
    seq: 0,
    events: [],
    panel: null,
    logEl: null,
    countEl: null,
    renderQueued: false,
  };

  function nowIso() {
    return new Date().toISOString();
  }

  function record(kind, data) {
    if (!state.enabled) return null;
    const event = {
      seq: ++state.seq,
      at: nowIso(),
      kind: String(kind || "event").slice(0, 40),
      data: redact(data === undefined ? {} : data),
    };
    state.events.push(event);
    if (state.events.length > MAX_EVENTS) {
      state.events.splice(0, state.events.length - MAX_EVENTS);
    }
    scheduleRender();
    return event;
  }

  function clear() {
    state.events = [];
    flushRender();
    return state.events.length;
  }

  function snapshot() {
    return {
      app: "Rave Player",
      kind: "rave-debug-log",
      version: 1,
      capturedAt: nowIso(),
      url: isBrowser() ? window.location.origin + window.location.pathname + window.location.hash : "",
      maxEvents: MAX_EVENTS,
      events: state.events,
    };
  }

  function formatText() {
    return state.events.map((event) => JSON.stringify(event)).join("\n");
  }

  /* ------------------------------------------------------------ panel */

  function button(label, act) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "debug-btn";
    el.dataset.act = act;
    el.textContent = label;
    return el;
  }

  function buildPanel() {
    if (!isBrowser() || state.panel) return;
    const panel = document.createElement("aside");
    panel.id = "debug-panel";
    panel.className = "debug-panel";
    panel.setAttribute("role", "region");
    panel.setAttribute("aria-label", "Debug log");

    const head = document.createElement("div");
    head.className = "debug-head";
    const title = document.createElement("span");
    title.className = "debug-title";
    title.textContent = "DEBUG";
    const count = document.createElement("span");
    count.className = "debug-count";
    count.id = "debug-count";
    count.textContent = "0 / " + MAX_EVENTS;
    const copy = button("Copy", "copy");
    const download = button("Download", "download");
    const clearBtn = button("Clear", "clear");
    const closeBtn = button("✕", "close");
    closeBtn.setAttribute("aria-label", "Close debug log");
    head.append(title, count, copy, download, clearBtn, closeBtn);

    const log = document.createElement("ol");
    log.className = "debug-log";
    log.id = "debug-log";

    const note = document.createElement("p");
    note.className = "debug-note";
    note.textContent = "Local only — never uploaded. Values redacted.";

    panel.append(head, log, note);
    document.body.appendChild(panel);

    copy.addEventListener("click", () => {
      copyText(formatText()).then(() => {
        copy.textContent = "Copied";
        setTimeout(() => { copy.textContent = "Copy"; }, 1200);
      }).catch(() => {
        copy.textContent = "Copy failed";
        setTimeout(() => { copy.textContent = "Copy"; }, 1600);
      });
    });
    download.addEventListener("click", downloadJSON);
    closeBtn.addEventListener("click", () => {
      setEnabled(false);
      syncToggle();
    });
    clearBtn.addEventListener("click", () => {
      clear();
      clearBtn.textContent = "Cleared";
      setTimeout(() => { clearBtn.textContent = "Clear"; }, 1000);
    });

    state.panel = panel;
    state.logEl = log;
    state.countEl = count;
    flushRender();
  }

  function lineFor(event) {
    const line = document.createElement("li");
    line.className = "debug-line";
    const kind = document.createElement("span");
    kind.className = "debug-kind";
    kind.textContent = event.kind;
    const body = document.createElement("code");
    body.className = "debug-body";
    let text = "";
    try {
      text = JSON.stringify(event.data);
    } catch (_) {
      text = "[unserialisable]";
    }
    body.textContent = (text || "").slice(0, 180);
    line.append(kind, body);
    return line;
  }

  function scheduleRender() {
    if (!isBrowser() || !state.panel || state.renderQueued) return;
    state.renderQueued = true;
    const run = () => flushRender();
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(run);
    } else {
      setTimeout(run, 16);
    }
  }

  function flushRender() {
    state.renderQueued = false;
    if (!state.panel || !state.logEl) return;
    const recent = state.events.slice(-MAX_RENDERED);
    const fragment = document.createDocumentFragment();
    recent.forEach((event) => fragment.appendChild(lineFor(event)));
    state.logEl.replaceChildren(fragment);
    if (state.countEl) {
      state.countEl.textContent = state.events.length + " / " + MAX_EVENTS;
    }
    state.logEl.scrollTop = state.logEl.scrollHeight;
  }

  /* ------------------------------------------------------------ menu toggle */

  function toggleButton() {
    if (!isBrowser()) return null;
    return document.getElementById("debug-toggle");
  }

  // The help dialog owns the visible switch; keep its label and ARIA state in
  // step with the URL/localStorage flag so it is correct on first paint too.
  function syncToggle() {
    const button = toggleButton();
    if (!button) return;
    const on = state.enabled;
    button.textContent = "Debug log: " + (on ? "on" : "off");
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.setAttribute("aria-label", "Debug log: " + (on ? "on" : "off")
      + (on ? " — turn off" : " — turn on"));
    button.classList.toggle("is-on", on);
  }

  // One path for the menu button: persist the flag, start/stop capture, and
  // drop the panel and buffered events on the way out.
  function setEnabled(on) {
    const enabled = !!on;
    if (enabled) {
      storageSet("1");
      state.enabled = true;
      if (!state.started) {
        start();
      } else {
        buildPanel();
        record("debug", { enabled: true, resumed: true });
      }
    } else {
      storageSet(null);
      state.enabled = false;
      clear();
      if (state.panel && state.panel.parentNode) {
        state.panel.parentNode.removeChild(state.panel);
      }
      state.panel = null;
      state.logEl = null;
      state.countEl = null;
      state.renderQueued = false;
    }
    syncToggle();
    return state.enabled;
  }

  function bindToggle() {
    const button = toggleButton();
    if (button && button.dataset.debugToggleBound !== "1") {
      button.dataset.debugToggleBound = "1";
      button.addEventListener("click", () => { setEnabled(!state.enabled); });
    }
    syncToggle();
  }

  /* ------------------------------------------------------------ actions */

  function copyText(text) {
    if (isBrowser() && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      if (!isBrowser()) {
        reject(new Error("no clipboard"));
        return;
      }
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "readonly");
      area.className = "debug-copy-area";
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (_) {
        ok = false;
      }
      area.remove();
      if (ok) resolve();
      else reject(new Error("copy unavailable"));
    });
  }

  function stamp() {
    return nowIso().replace(/[-:]/g, "").replace(/\..*$/, "").replace("T", "-");
  }

  function downloadJSON() {
    if (!isBrowser()) return;
    const blob = new Blob([JSON.stringify(snapshot(), null, 2)],
                          { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "rave-debug-" + stamp() + ".json";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  /* ------------------------------------------------------------ capture */

  function safePath(value) {
    if (!value) return "";
    if (String(value).slice(0, 5) === "blob:") return "[blob]";
    try {
      const url = new URL(String(value), window.location.href);
      return url.pathname;
    } catch (_) {
      return redactString(value);
    }
  }

  function requestId() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID().replace(/-/g, "");
      }
    } catch (_) { /* fall through */ }
    return "d" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  function plainHeaders(headers) {
    const out = {};
    try {
      if (!headers) return out;
      if (typeof Headers !== "undefined" && headers instanceof Headers) {
        headers.forEach((value, name) => { out[name] = value; });
        return out;
      }
      if (Array.isArray(headers)) {
        headers.forEach((pair) => {
          if (pair && pair.length === 2) out[pair[0]] = pair[1];
        });
        return out;
      }
      return Object.assign(out, headers);
    } catch (_) {
      return out;
    }
  }

  function setHeader(headers, name, value) {
    try {
      if (headers instanceof Headers) {
        headers.set(name, value);
        return headers;
      }
      if (Array.isArray(headers)) {
        headers.push([name, value]);
        return headers;
      }
      headers[name] = value;
      return headers;
    } catch (_) {
      return headers;
    }
  }

  function wrapFetch() {
    if (typeof window.fetch !== "function" || window.fetch.__raveDebug) return;
    const original = window.fetch;
    const wrapped = function (input, init) {
      let url;
      try {
        url = new URL(typeof input === "string" ? input : input.url, window.location.href);
      } catch (_) {
        return original.apply(this, arguments);
      }
      if (url.origin !== window.location.origin) return original.apply(this, arguments);
      const options = Object.assign({}, init || {});
      const method = String(options.method || (input && input.method) || "GET").toUpperCase();
      const id = requestId();
      const existing = options.headers
        || (input && typeof input !== "string" && input.headers) || null;
      options.headers = setHeader(plainHeaders(existing), "X-Request-ID", id);
      const started = (window.performance && performance.now) ? performance.now() : Date.now();
      const elapsed = () => Math.round(((window.performance && performance.now)
        ? performance.now() : Date.now()) - started);
      return original.call(this, input, options).then((response) => {
        record("api", {
          path: url.pathname,
          method: method,
          status: response.status,
          ms: elapsed(),
          requestId: response.headers.get("X-Request-ID") || id,
        });
        return response;
      }).catch((error) => {
        record("api", {
          path: url.pathname,
          method: method,
          status: 0,
          ms: elapsed(),
          error: (error && error.name) || "error",
        });
        throw error;
      });
    };
    wrapped.__raveDebug = true;
    window.fetch = wrapped;
  }

  const MEDIA_EVENTS = ["play", "pause", "playing", "waiting", "ended", "error",
                        "loadedmetadata", "seeking", "seeked", "stalled"];

  function onMedia(event) {
    const el = event.target;
    if (!el || el.tagName !== "AUDIO") return;
    record("media", {
      event: event.type,
      path: safePath(el.currentSrc || el.src),
      at: Math.round(Number(el.currentTime) || 0),
      paused: !!el.paused,
      readyState: Number(el.readyState) || 0,
    });
  }

  function start() {
    if (!isBrowser() || state.started || !state.enabled) return;
    state.started = true;
    buildPanel();
    record("debug", {
      enabled: true,
      buffer: MAX_EVENTS,
      visibility: document.visibilityState || "",
      online: navigator.onLine !== false,
    });
    record("route", { path: window.location.pathname, initial: true });

    wrapFetch();

    window.addEventListener("error", (event) => {
      const target = event.target;
      if (target && target !== window && target.tagName) {
        if (target.tagName === "AUDIO") return;  // the media listener covers it
        record("error", {
          kind: "resource",
          tag: String(target.tagName).toLowerCase(),
          path: safePath(target.currentSrc || target.src),
        });
        return;
      }
      record("error", {
        kind: "uncaught",
        message: event.message || "",
        path: safePath(event.filename),
        line: Number(event.lineno) || 0,
      });
    }, true);

    window.addEventListener("unhandledrejection", (event) => {
      const reason = event.reason;
      record("error", {
        kind: "rejection",
        message: (reason && reason.message) ? reason.message : String(reason),
      });
    });

    document.addEventListener("visibilitychange", () => {
      record("visibility", { state: document.visibilityState || "" });
    });
    window.addEventListener("online", () => record("network", { online: true }));
    window.addEventListener("offline", () => record("network", { online: false }));

    MEDIA_EVENTS.forEach((name) => {
      document.addEventListener(name, onMedia, true);
    });

    document.addEventListener("rave:sw", (event) => {
      const detail = (event && event.detail) || {};
      record("sw", {
        event: detail.state || "state",
        scope: safePath(detail.scope),
        worker: detail.worker || "",
      });
    });

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        record("sw", { event: "controllerchange", controller: !!navigator.serviceWorker.controller });
      });
      const describe = (label) => {
        if (typeof navigator.serviceWorker.getRegistration !== "function") return;
        navigator.serviceWorker.getRegistration().then((reg) => {
          record("sw", {
            event: label,
            scope: reg ? safePath(reg.scope) : "",
            active: (reg && reg.active) ? reg.active.state : "none",
            waiting: !!(reg && reg.waiting),
            installing: !!(reg && reg.installing),
          });
        }).catch(() => { /* no registration */ });
      };
      describe("ready");
      if (navigator.serviceWorker.ready && navigator.serviceWorker.ready.then) {
        navigator.serviceWorker.ready.then(() => describe("activated")).catch(() => {});
      }
    }
  }

  /* ------------------------------------------------------------ boot */

  const api = {
    STORAGE_KEY: STORAGE_KEY,
    MAX_EVENTS: MAX_EVENTS,
    resolveEnabled: resolveEnabled,
    flagFromSearch: flagFromSearch,
    redact: (value) => redact(value),
    record: record,
    clear: clear,
    snapshot: snapshot,
    formatText: formatText,
    copyText: copyText,
    start: start,
    setEnabled: setEnabled,
    enable: () => setEnabled(true),
    disable: () => setEnabled(false),
  };
  Object.defineProperty(api, "enabled", { get: () => state.enabled });
  Object.defineProperty(api, "size", { get: () => state.events.length });

  if (isBrowser()) {
    const explicit = flagFromSearch(window.location.search);
    if (explicit !== null) storageSet(explicit ? "1" : null);
    state.enabled = resolveEnabled(window.location.search, storageGet());
    if (state.enabled) start();
    window.RaveDebug = api;
    bindToggle();
  }

  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root && !root.RaveDebug) {
    root.RaveDebug = api;
  }
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
