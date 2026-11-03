/* Rave Player — provider widget bridge (1.6.45).
 *
 * SoundCloud and Mixcloud both expose a JavaScript API, but each API script is
 * served from the provider's own origin (w.soundcloud.com/player/api.js and
 * widget.mixcloud.com/media/js/widgetApi.js).  This app keeps the deliberate
 * CSP `script-src 'self'`, so those scripts cannot be loaded.  Both APIs are
 * nevertheless postMessage protocols, and the widgets announce themselves as
 * soon as their documents load.  This file implements the small subset needed
 * to observe play/pause/ended and to prove the widget is alive:
 *
 *   SoundCloud: {method:"ready"}                        <- widget
 *               {method:"addEventListener",value:"play"} -> widget
 *               {method:"play"|"pause"|"finish"|"error"}  <- widget
 *   Mixcloud:   {mixcloud:"playerWidget",type:"ready"}    <- widget
 *               {type:"getApi"}                           -> widget
 *               {mixcloud:"playerWidget",type:"event",
 *                data:{eventName:"play"|"pause"|"ended"|...}} <- widget
 *
 * Cross-origin limits (documented in docs/AUDIO-SINGLE-SOURCE.md):
 *   - The parent can never read the iframe DOM, its media element, cookies or
 *     autoplay policy.  All state below is reported by the widget itself.
 *   - Messages are accepted only when event.source is our iframe and the
 *     origin is an exact allowed provider origin.  We never use "*" as a
 *     target origin.
 *   - Mixcloud's widget redirects from www.mixcloud.com to
 *     player-widget.mixcloud.com; both origins are allowed and commands are
 *     posted to each (the wrong one is dropped by the browser).
 *   - The provider protocols are not a stability contract.  If a provider
 *     changes one, the bridge fails soft (no events) and the single-audio rule
 *     is still enforced at every app boundary: opening an embed stops the main
 *     player, and starting main playback removes the iframe.
 *   - Keyboard events inside the cross-origin iframe do not reach this page;
 *     Escape closes the dock only while focus is in the parent document.  The
 *     visible Close button is always available.
 *
 * The bridge is plain ES5 and has no DOM dependency beyond the frame and
 * window passed in, so test/js/embed_spec.js can exercise it under Node.
 */
(function (root, factory) {
  "use strict";
  var api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RaveEmbed = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  var ORIGINS = {
    soundcloud: ["https://w.soundcloud.com"],
    mixcloud: [
      "https://www.mixcloud.com",
      "https://player-widget.mixcloud.com",
    ],
  };

  var LABELS = { soundcloud: "SoundCloud", mixcloud: "Mixcloud" };

  function providerKey(provider) {
    var key = String(provider || "").toLowerCase();
    return Object.prototype.hasOwnProperty.call(ORIGINS, key) ? key : "";
  }

  function origins(provider) {
    var key = providerKey(provider);
    return key ? ORIGINS[key].slice() : [];
  }

  function originAllowed(provider, origin) {
    var key = providerKey(provider);
    var value = String(origin || "").replace(/\/+$/, "");
    return !!key && origins(key).indexOf(value) !== -1;
  }

  function providerLabel(provider) {
    return LABELS[providerKey(provider)] || "Provider";
  }

  /* Parse only messages that look like the provider's own protocol.  Anything
   * else (including other pages posting to the window) returns null. */
  function parseWidgetMessage(provider, data) {
    var key = providerKey(provider);
    if (!key || typeof data !== "string") return null;
    var payload;
    try { payload = JSON.parse(data); } catch (_) { return null; }
    if (!payload || typeof payload !== "object") return null;

    if (key === "soundcloud") {
      if (typeof payload.method !== "string") return null;
      return { provider: key, type: payload.method, value: payload.value };
    }

    if (payload.mixcloud !== "playerWidget" || typeof payload.type !== "string") {
      return null;
    }
    if (payload.type === "event") {
      var event = payload.data;
      if (!event || typeof event.eventName !== "string") return null;
      return { provider: key, type: event.eventName, value: event.args };
    }
    if (payload.type === "api") {
      return { provider: key, type: "api", value: payload.data };
    }
    return { provider: key, type: payload.type };
  }

  /* Map provider event names onto the two states the app cares about. */
  function eventKind(provider, type) {
    var key = providerKey(provider);
    if (key === "soundcloud") {
      if (type === "play") return "play";
      if (type === "pause") return "pause";
      if (type === "finish") return "ended";
      if (type === "error") return "error";
      return "";
    }
    if (key === "mixcloud") {
      if (type === "play" || type === "pause" || type === "ended" || type === "error") {
        return type;
      }
    }
    return "";
  }

  /* Commands sent on iframe load and again when the widget says "ready".
   * SoundCloud needs explicit listeners; Mixcloud answers a getApi handshake. */
  function commands(provider) {
    var key = providerKey(provider);
    if (key === "soundcloud") {
      return [
        { method: "addEventListener", value: "play" },
        { method: "addEventListener", value: "pause" },
        { method: "addEventListener", value: "finish" },
      ];
    }
    if (key === "mixcloud") return [{ type: "getApi" }];
    return [];
  }

  function attach(options) {
    var opts = options || {};
    var key = providerKey(opts.provider);
    var frame = opts.frame;
    var win = opts.window || root;
    var onEvent = typeof opts.onEvent === "function" ? opts.onEvent : function () {};
    var onReady = typeof opts.onReady === "function" ? opts.onReady : function () {};
    var destroyed = false;

    if (!key || !frame || !win || typeof win.addEventListener !== "function") {
      return { provider: key, sync: function () {}, destroy: function () {} };
    }

    function post(message) {
      if (destroyed || !frame.contentWindow) return;
      var payload = JSON.stringify(message);
      origins(key).forEach(function (origin) {
        try { frame.contentWindow.postMessage(payload, origin); } catch (_) { /* frame gone */ }
      });
    }

    function sync() {
      commands(key).forEach(post);
    }

    function handle(event) {
      if (destroyed || !frame.contentWindow) return;
      /* Source and origin are both checked: origin alone would accept any
       * other SoundCloud/Mixcloud frame on the page, and source alone would
       * accept a same-origin page framed by the widget. */
      if (event.source !== frame.contentWindow) return;
      if (!originAllowed(key, event.origin)) return;
      var message = parseWidgetMessage(key, event.data);
      if (!message) return;
      if (message.type === "ready") {
        sync();
        onReady(message);
        return;
      }
      if (message.type === "api") return; // Mixcloud handshake complete
      var kind = eventKind(key, message.type);
      if (kind) onEvent(kind, message);
    }

    win.addEventListener("message", handle);
    return {
      provider: key,
      sync: sync,
      destroy: function () {
        if (destroyed) return;
        destroyed = true;
        try { win.removeEventListener("message", handle); } catch (_) { /* ignore */ }
      },
    };
  }

  return {
    attach: attach,
    parseWidgetMessage: parseWidgetMessage,
    originAllowed: originAllowed,
    origins: origins,
    providerLabel: providerLabel,
    eventKind: eventKind,
    commands: commands,
  };
});
