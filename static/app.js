/* Rave Player web UI: hash-routed lists, a queue-driven player, favourites. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const listEl = $("list");
  const titleEl = $("title");
  const navEl = $("nav");
  const audio = $("audio");
  const player = $("player");
  const moreEl = $("more");
  const emptyEl = $("empty");
  const stateEl = $("state");
  const stateText = $("state-text");
  const stateAction = $("state-action");
  const bannerEl = $("banner");
  const queueEl = $("queue");
  const helpEl = $("help");
  const rangeEl = $("p-range");
  const searchInput = $("search-input");

  const DEFAULT_EMPTY = "Nothing here — try another section.";
  const SEARCH_EMPTY = "Search the archive — type a query above, or press / to jump to the search box.";

  const state = {
    path: "",
    items: [],
    next: null,
    title: "",
    queue: [],
    queueSource: "",
    index: -1,
    current: null,
    currentKey: "",
    faved: new Set(),
    seq: 0,
    depth: 0,
  };

  const MEDIA_SESSION = "mediaSession" in navigator && typeof MediaMetadata !== "undefined";
  // iPadOS reports itself as a Mac; the touch-point count is the reliable tell.
  const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  // iOS ignores HTMLMediaElement.volume: the hardware buttons (or the AirPlay
  // receiver) own the output level. The player therefore has no volume slider,
  // and none should be added unless it is hidden when IS_IOS is true.


  /* ------------------------------------------------------------ icons */
  // Icons are built as DOM nodes so the UI has a single code path and no
  // injection surface. Emoji glyphs were removed because the media symbols
  // render inconsistently across phones.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICON_PATHS = {
    play: [{ tag: "path", attrs: { d: "M8 5.4v13.2L19 12 8 5.4Z", fill: "currentColor" } }],
    pause: [
      { tag: "rect", attrs: { x: "7", y: "5", width: "3.6", height: "14", rx: "1.3", fill: "currentColor" } },
      { tag: "rect", attrs: { x: "13.4", y: "5", width: "3.6", height: "14", rx: "1.3", fill: "currentColor" } },
    ],
    chevron: [{ tag: "path", attrs: { d: "M9 5l7 7-7 7", fill: "none", stroke: "currentColor",
                                      "stroke-width": "2.2", "stroke-linecap": "round", "stroke-linejoin": "round" } }],
    close: [{ tag: "path", attrs: { d: "M6 6l12 12M18 6L6 18", fill: "none", stroke: "currentColor",
                                    "stroke-width": "2.2", "stroke-linecap": "round" } }],
    heart: [{ tag: "path", attrs: { class: "heart-path", fill: "none", stroke: "currentColor", "stroke-width": "1.9",
                                    d: "M12 20s-7-4.6-7-9.4C5 7.5 6.9 6 9 6c1.5 0 2.6.8 3 1.7.4-.9 1.5-1.7 3-1.7 2.1 0 4 1.5 4 4.6C19 15.4 12 20 12 20Z" } }],
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "ic");
    (ICON_PATHS[name] || []).forEach((spec) => {
      const node = document.createElementNS(SVG_NS, spec.tag);
      Object.entries(spec.attrs).forEach(([key, value]) => node.setAttribute(key, value));
      svg.appendChild(node);
    });
    return svg;
  }

  function setIcon(target, name) {
    target.replaceChildren(icon(name));
  }

  /* ------------------------------------------------------------ airplay */

  // Safari exposes AirPlay through WebKit-only properties; Chromium exposes
  // the Remote Playback API (Cast). Both are optional and feature-detected.
  //
  // What WebKit guarantees (Source/WebCore/html/HTMLMediaElement.cpp):
  //   * adding a `webkitplaybacktargetavailabilitychanged` listener enqueues an
  //     initial event, and the event fires again when the media player is
  //     prepared and whenever route availability changes;
  //   * `webkitCurrentPlaybackTargetIsWireless` (and the matching ...changed
  //     event) are the only honest source of "is audio on the other device";
  //   * `webkitShowPlaybackTargetPicker()` must run inside a user gesture on
  //     iOS (`RequireUserGestureToShowPlaybackTargetPicker`) and macOS refuses
  //     it until the element has metadata, so we only call it from the click
  //     handler after a track is loaded.
  const AIRPLAY = {
    kind: "",          // "webkit" (Safari) | "remote" (Chromium)
    available: null,   // null = not reported yet; true/false from the browser
    connected: false,
    remote: null,
    watchId: null,
  };

  // The receiver fetches the stream URL itself. That makes the page's origin
  // part of the AirPlay contract: an Apple TV can reach a LAN address, but not
  // the iPhone's localhost or (usually) a Tailscale-only 100.x address.
  function airplayReachabilityHint() {
    const appAddress = "http://192.0.2.10:8097";
    const host = (window.location.hostname || "").replace(/^\[|\]$/g, "");
    if (window.location.protocol === "file:") {
      return `An AirPlay device can't fetch audio from a local file — open ${appAddress}.`;
    }
    if (host === "localhost" || host === "127.0.0.1" || host === "::1" || /\.localhost$/i.test(host)) {
      return `The TV can't fetch audio from "${host}". Open ${appAddress} on the iPhone instead.`;
    }
    if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host) || /\.ts\.net$/i.test(host)) {
      return "This page is open over Tailscale. AirPlay only works if the TV is on the tailnet too — otherwise use the LAN address (192.0.2.10:8097).";
    }
    return "";
  }

  function airplayDeviceLabel() {
    return IS_IOS ? "Apple TV or HomePod" : "AirPlay or Cast device";
  }

  function airplayIsWireless() {
    if (AIRPLAY.kind === "webkit" && typeof audio.webkitCurrentPlaybackTargetIsWireless === "boolean") {
      return audio.webkitCurrentPlaybackTargetIsWireless;
    }
    if (AIRPLAY.kind === "remote" && AIRPLAY.remote) return AIRPLAY.remote.state === "connected";
    return AIRPLAY.connected;
  }

  // The icon is visible whenever a track is loaded and the browser has an
  // AirPlay/Cast API. It is dimmed with aria-disabled (not hidden and not a
  // silently dead button) when WebKit says no route is available; a tap then
  // explains why instead of guessing a state.
  function refreshAirPlayButton() {
    const button = $("p-airplay");
    if (!button) return;
    AIRPLAY.connected = airplayIsWireless();
    const loaded = !!state.current && !!(audio.getAttribute("src") || audio.currentSrc);
    const show = !!AIRPLAY.kind && loaded;
    button.hidden = !show;
    if (!show) return;
    const unavailable = AIRPLAY.available === false && !AIRPLAY.connected;
    button.classList.toggle("connected", AIRPLAY.connected);
    button.classList.toggle("unavailable", unavailable);
    button.setAttribute("aria-pressed", String(AIRPLAY.connected));
    button.setAttribute("aria-disabled", String(unavailable));
    button.title = AIRPLAY.connected
      ? "AirPlay connected — tap to change or stop"
      : unavailable ? "No AirPlay devices found — tap for details"
      : "Play on AirPlay";
    button.setAttribute("aria-label", AIRPLAY.connected
      ? "AirPlay connected — change device"
      : unavailable ? "AirPlay — no devices found"
      : "AirPlay");
  }

  function onAirPlayClick() {
    if (!state.current || !(audio.getAttribute("src") || audio.currentSrc)) {
      toast("Play a track before choosing AirPlay");
      return;
    }
    const hint = airplayReachabilityHint();
    const noRoute = AIRPLAY.available === false && !AIRPLAY.connected;
    if (noRoute) {
      toast(hint
        ? `${hint} No ${airplayDeviceLabel()} found.`
        : `No ${airplayDeviceLabel()} found. Check it is awake and on the same Wi-Fi.`, 6500);
      return;
    }
    if (hint) toast(hint, 6500);
    if (AIRPLAY.kind === "webkit") {
      // macOS refuses the picker until the element has metadata; iOS has no
      // such requirement. Stay inside the gesture for the actual call.
      if (!IS_IOS && audio.readyState < 1) {
        toast("The track is still loading — try AirPlay again in a moment");
        return;
      }
      try {
        audio.webkitShowPlaybackTargetPicker();
      } catch (_) {
        toast("AirPlay isn't available right now");
      }
      return;
    }
    if (AIRPLAY.kind === "remote" && AIRPLAY.remote) {
      try {
        const prompted = AIRPLAY.remote.prompt();
        if (prompted && typeof prompted.catch === "function") {
          prompted.catch((error) => {
            if (error && error.name === "NotAllowedError") toast("AirPlay needs a tap — try again");
            else toast(`No ${airplayDeviceLabel()} found`);
          });
        }
      } catch (_) {
        toast(`No ${airplayDeviceLabel()} found`);
      }
    }
  }

  // SoundCloud's API needs a paid app, so pasting a set URL is the way to
  // play anything outside the curated list. Same for Mixcloud.
  function externalEmbed(raw) {
    const value = String(raw || "").trim();
    let match = value.match(/^https?:\/\/(?:www\.)?soundcloud\.com\/\S+$/i);
    if (match) {
      return {
        label: "SoundCloud set",
        external: value,
        embed: `https://w.soundcloud.com/player/?url=${encodeURIComponent(value)}` +
               "&color=%23e8ff00&auto_play=false&hide_related=1&show_comments=0&visual=0",
      };
    }
    match = value.match(/^https?:\/\/(?:www\.)?mixcloud\.com\/\S+$/i);
    if (match) {
      const page = value.split("?")[0];
      return {
        label: "Mixcloud set",
        external: page,
        embed: "https://www.mixcloud.com/widget/iframe/?hide_cover=1&mini=1&light=0&feed=" +
               encodeURIComponent(page),
      };
    }
    return null;
  }

  function setupRemotePlayback() {
    const button = $("p-airplay");
    if (!button) return;
    button.hidden = true;
    button.addEventListener("click", onAirPlayClick);

    if (typeof audio.webkitShowPlaybackTargetPicker === "function") {
      AIRPLAY.kind = "webkit";
      audio.addEventListener("webkitplaybacktargetavailabilitychanged", (event) => {
        AIRPLAY.available = event.availability === "available";
        refreshAirPlayButton();
      });
      audio.addEventListener("webkitcurrentplaybacktargetiswirelesschanged", () => {
        const connected = airplayIsWireless();
        const changed = connected !== AIRPLAY.connected;
        AIRPLAY.connected = connected;
        refreshAirPlayButton();
        if (changed && state.current) {
          toast(connected ? "Playing on AirPlay" : "AirPlay disconnected — back on this device");
        }
      });
      return;
    }

    const remote = audio.remote;
    if (!remote || typeof remote.prompt !== "function") return;
    AIRPLAY.kind = "remote";
    AIRPLAY.remote = remote;
    if (typeof remote.addEventListener === "function") {
      const sync = () => {
        const before = AIRPLAY.connected;
        AIRPLAY.connected = remote.state === "connected";
        refreshAirPlayButton();
        if (before !== AIRPLAY.connected && state.current) {
          toast(AIRPLAY.connected ? "Playing on AirPlay/Cast" : "AirPlay/Cast disconnected");
        }
      };
      remote.addEventListener("connect", sync);
      remote.addEventListener("disconnect", sync);
    }
    if (typeof remote.watchAvailability === "function") {
      let watching = null;
      try {
        watching = remote.watchAvailability((available) => {
          AIRPLAY.available = !!available;
          refreshAirPlayButton();
        });
      } catch (_) {
        AIRPLAY.available = null;
      }
      if (watching && typeof watching.then === "function") {
        watching.then((id) => {
          AIRPLAY.watchId = id;
        }).catch(() => {
          // Chromium can refuse availability watching (e.g. private mode). Leave
          // the state unknown and let prompt() report "no devices" itself.
          AIRPLAY.available = null;
          refreshAirPlayButton();
        });
      }
    }
  }

  /* ------------------------------------------------------------ utilities */

  function toast(message, ms = 2600) {
    const el = $("toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, ms);
  }

  function setStatus(text) {
    $("status").textContent = text;
  }

  function fmtTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
             : `${m}:${String(s).padStart(2, "0")}`;
  }

  function playable(item) {
    return !!(item && (item.play || item.play_via));
  }

  function openEmbed(item) {
    const panel = $("embed-panel");
    const frame = $("embed-frame");
    const title = $("embed-title");
    const external = $("embed-external");
    if (!panel || !frame) return;
    // Route and label the panel from the item itself: a pasted SoundCloud URL
    // must not open with "Open on Mixcloud" / "Mixcloud player".
    const isSoundCloud = /soundcloud\.com/i.test(String(item.external || item.embed || ""));
    const provider = isSoundCloud ? "SoundCloud" : "Mixcloud";
    title.textContent = item.label || provider;
    external.href = item.external || (isSoundCloud ? "https://soundcloud.com/" : "https://www.mixcloud.com/");
    external.textContent = `Open on ${provider} ↗`;
    frame.title = `${provider} player`;
    const wrap = panel.querySelector(".embed-frame-wrap");
    if (wrap) { wrap.classList.add("is-loading"); wrap.classList.remove("is-loaded"); }
    if (frame.getAttribute("src") !== item.embed) frame.setAttribute("src", item.embed);
    else if (wrap) { wrap.classList.remove("is-loading"); wrap.classList.add("is-loaded"); }
    panel.hidden = false;
    document.body.classList.add("has-embed");
    $("embed-close").focus();
  }

  function markEmbedLoaded() {
    const wrap = document.querySelector("#embed-panel .embed-frame-wrap");
    if (!wrap) return;
    wrap.classList.remove("is-loading");
    wrap.classList.add("is-loaded");
  }

  function closeEmbed() {
    const panel = $("embed-panel");
    const frame = $("embed-frame");
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    if (frame) frame.removeAttribute("src");
    document.body.classList.remove("has-embed");
  }

  function favKey(fav) {
    return `${fav.kind}:${fav.target}`;
  }

  function itemKey(item) {
    if (!item) return "";
    if (item.fav && item.fav.kind && item.fav.target) return `fav:${favKey(item.fav)}`;
    return `url:${item.play || item.play_via || item.open || ""}|${item.label || ""}`;
  }

  function messageFor(error) {
    if (!error) return "unknown error";
    if (error.offline) return "you're offline";
    if (error.timeout) return "the request timed out";
    return error.message || "request failed";
  }

  function absolutize(src) {
    try { return new URL(src, window.location.href).href; } catch (_) { return src; }
  }

  /* ------------------------------------------------------------ data */

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 40000);
    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        headers: Object.assign({ Accept: "application/json" }, options.headers || {}),
        body: options.body,
        signal: controller.signal,
        cache: "no-store",
      });
      let data = null;
      try { data = await response.json(); } catch (_) { data = null; }
      if (!response.ok) {
        const error = new Error((data && data.error) || `HTTP ${response.status}`);
        error.status = response.status;
        error.payload = data;
        throw error;
      }
      return data || {};
    } catch (error) {
      if (error.name === "AbortError") {
        const timeout = new Error("Request timed out");
        timeout.timeout = true;
        throw timeout;
      }
      if (error instanceof TypeError) {
        error.offline = !navigator.onLine;
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  /* ------------------------------------------------------------ state blocks */

  function showState(kind, message, retry) {
    stateEl.hidden = false;
    stateEl.classList.toggle("is-error", kind === "error");
    stateText.textContent = "";
    if (kind === "loading") {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      stateText.appendChild(spinner);
      stateText.appendChild(document.createTextNode(" " + message));
    } else {
      stateText.textContent = message;
    }
    stateAction.hidden = !retry;
    stateAction.onclick = retry || null;
  }

  function hideState() {
    stateEl.hidden = true;
    stateAction.onclick = null;
  }

  function showSkeleton() {
    listEl.textContent = "";
    listEl.setAttribute("aria-busy", "true");
    for (let i = 0; i < 6; i += 1) {
      const row = document.createElement("div");
      row.className = "row skeleton";
      row.setAttribute("aria-hidden", "true");
      const art = document.createElement("div");
      art.className = "row-art";
      const text = document.createElement("div");
      text.className = "row-text";
      const bar = document.createElement("div");
      bar.className = "bar";
      const shortBar = document.createElement("div");
      shortBar.className = "bar short";
      text.append(bar, shortBar);
      const oval = document.createElement("div");
      oval.className = "bar oval";
      row.append(art, text, oval);
      listEl.appendChild(row);
    }
  }

  function describeError(error) {
    if (error.offline) return "You're offline. Reconnect, then try again.";
    if (error.timeout) return "The server took too long to answer. Try again.";
    if (error.status === 502 || error.status === 504) {
      return "The source didn't respond — it may be down right now. Try again in a moment.";
    }
    if (error.status === 503) return "The server is busy right now. Try again in a few seconds.";
    if (error.status === 404) return "That section or item wasn't found.";
    return `Could not load this section (${messageFor(error)}).`;
  }

  function showBanner(message) {
    bannerEl.textContent = message;
    bannerEl.hidden = false;
  }

  function hideBanner() {
    bannerEl.hidden = true;
  }

  /* ------------------------------------------------------------ rendering */

  function render(items) {
    const fragment = document.createDocumentFragment();
    items.forEach((item) => fragment.appendChild(row(item)));
    listEl.textContent = "";
    listEl.appendChild(fragment);
    listEl.setAttribute("aria-busy", "false");
    emptyEl.textContent = DEFAULT_EMPTY;
    emptyEl.hidden = items.length > 0;
    paintNowPlaying();
  }

  function row(item) {
    const el = document.createElement("div");
    el.className = "row";
    el.dataset.key = itemKey(item);
    el.dataset.label = item.label || "item";
    const actionWord = item.play || item.play_via ? "Play" : "Open";
    el.dataset.action = actionWord;

    const art = document.createElement("img");
    art.className = "row-art";
    art.loading = "lazy";
    art.decoding = "async";
    art.width = 52;
    art.height = 52;
    art.alt = "";
    art.src = item.art || "/static/art/tapepacks.png";
    art.addEventListener("error", () => { art.src = "/static/art/tapepacks.png"; }, { once: true });

    const text = document.createElement("div");
    text.className = "row-text";
    const label = document.createElement("div");
    label.className = "row-label";
    label.textContent = item.label || "Untitled";
    const sub = document.createElement("div");
    sub.className = "row-sub";
    sub.textContent = item.sub || "";
    text.append(label, sub);

    const actions = document.createElement("div");
    actions.className = "row-actions";

    if (item.fav) {
      const key = favKey(item.fav);
      const on = state.faved.has(key);
      const heart = document.createElement("button");
      heart.type = "button";
      heart.className = "icon-btn" + (on ? " faved" : "");
      heart.dataset.favKey = key;
      setIcon(heart, "heart");
      heart.title = on ? "Remove from favourites" : "Add to favourites";
      heart.setAttribute("aria-label", `${on ? "Remove" : "Add"} ${item.label || "item"} ${on ? "from" : "to"} favourites`);
      heart.setAttribute("aria-pressed", String(on));
      heart.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleFav(item.fav, heart);
      });
      actions.appendChild(heart);
    }

    if (item.fav_key) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "icon-btn";
      setIcon(remove, "close");
      remove.title = "Remove from favourites";
      remove.setAttribute("aria-label", `Remove ${item.label || "item"} from favourites`);
      remove.addEventListener("click", async (event) => {
        event.stopPropagation();
        remove.disabled = true;
        try {
          const data = await api(`/api/favourites/${encodeURIComponent(item.fav_key)}`, { method: "DELETE" });
          if (data.removed) {
            el.remove();
            state.faved.delete(item.fav_key);
            if (!listEl.querySelector(".row")) emptyEl.hidden = false;
            toast("Removed from favourites");
          } else {
            remove.disabled = false;
            toast("Could not remove that favourite");
          }
        } catch (error) {
          remove.disabled = false;
          toast(`Could not remove: ${messageFor(error)}`, 4000);
        }
      });
      actions.appendChild(remove);
    }

    const action = document.createElement("button");
    action.type = "button";
    action.className = "icon-btn" + (playable(item) ? " play" : "");
    setIcon(action, playable(item) ? "play" : "chevron");
    action.title = playable(item) ? "Play" : "Open";
    action.setAttribute("aria-label", `${actionWord} ${item.label || "item"}`);
    action.addEventListener("click", (event) => {
      event.stopPropagation();
      activate(item);
    });

    el.append(art, text, actions);
    actions.appendChild(action);

    function activate() {
      if (item.embed) { openEmbed(item); return; }
      if (item.play_via) {
        if (state.currentKey === itemKey(item) && audio.getAttribute("src")) { togglePlay(); return; }
        resolveAndPlay(item);
        return;
      }
      if (item.play) {
        if (state.currentKey === itemKey(item) && audio.getAttribute("src")) { togglePlay(); return; }
        playItem(item, item.play);
        return;
      }
      if (item.open) navigate(item.open);
    }

    el.addEventListener("click", activate);
    return el;
  }

  function paintNowPlaying() {
    // Called on every play/pause and after every render. Only touch the rows
    // whose state changed: rebuilding an SVG for hundreds of rows on each
    // pause is a visible stall on a phone.
    [...listEl.children].forEach((el) => {
      if (!el.classList || !el.classList.contains("row")) return;
      const playing = !!state.currentKey && el.dataset.key === state.currentKey;
      const wasPlaying = el.dataset.nowplaying === "1";
      if (playing !== wasPlaying) {
        el.classList.toggle("playing", playing);
        if (playing) el.setAttribute("aria-current", "true");
        else el.removeAttribute("aria-current");
        el.dataset.nowplaying = playing ? "1" : "0";
      }
      const button = el.querySelector(".icon-btn.play");
      if (!button) return;
      const paused = audio.paused;
      const iconState = playing ? (paused ? "play" : "pause") : "play";
      if (button.dataset.iconState !== iconState) {
        setIcon(button, iconState);
        button.dataset.iconState = iconState;
      }
      const word = playing ? (paused ? "Resume" : "Pause") : (el.dataset.action || "Play");
      const label = `${word} ${el.dataset.label || ""}`.trim();
      if (button.getAttribute("aria-label") !== label) button.setAttribute("aria-label", label);
    });
  }

  /* ------------------------------------------------------------ navigation */

  function sectionPath(id) {
    const map = {
      latest: "/api/latest",
      kool: "/api/filter/kool",
      filters: "/api/filters",
      live: "/api/live",
      packs: "/api/packs",
      podcasts: "/api/podcasts",
      dnb: "/api/dnb",
      ukg: "/api/ukg",
      years: "/api/years",
      mixcloud: "/api/mixcloud_hub",
      soundcloud: "/api/soundcloud",
      raves: "/api/raves",
      legacy: "/api/legacy",
      favourites: "/api/favourites",
      search: "/api/search",
    };
    return map[id] || "";
  }

  function normalizePath(value) {
    if (typeof value !== "string") return "";
    let path = value.trim();
    if (!path) return "";
    if (path.startsWith("#")) path = path.slice(1);
    if (path.startsWith("/api/")) return path;
    if (path.startsWith("/")) return sectionPath(path.slice(1));
    return sectionPath(path);
  }

  function pathFromHash() {
    const raw = window.location.hash.replace(/^#/, "");
    if (!raw) return "";
    if (/^%2f/i.test(raw)) {
      try { return normalizePath(decodeURIComponent(raw)); } catch (_) { return ""; }
    }
    return normalizePath(raw);
  }

  function labelForPath(path) {
    const button = [...navEl.querySelectorAll("button")].find((entry) => entry.dataset.path === path);
    return button ? button.textContent.trim() : "";
  }

  function highlightNav(path) {
    navEl.querySelectorAll("button").forEach((button) => {
      const active = button.dataset.path === path;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  function scrollTop() {
    if (window.scrollY > 0) window.scrollTo(0, 0);
  }

  // iOS Safari's own scroll restoration fights a hash-routed SPA, so it is
  // switched off at boot and each history entry remembers where it was left.
  function saveCurrentScroll() {
    const current = history.state;
    if (!current || typeof current.path !== "string") return;
    const scrollY = Math.max(0, Math.round(window.scrollY));
    if (current.scrollY === scrollY) return;
    try {
      history.replaceState(Object.assign({}, current, { scrollY }), "", window.location.href);
    } catch (_) { /* ignore */ }
  }

  function pushHistory(path) {
    const depth = (Number(history.state && history.state.depth) || 0) + 1;
    state.depth = depth;
    try {
      history.pushState({ path, depth, scrollY: 0 }, "", "#" + path);
    } catch (_) {
      window.location.hash = path;
    }
    $("back").hidden = depth <= 0;
  }

  async function navigate(path, options = {}) {
    const push = options.push !== false;
    if (push) saveCurrentScroll();
    path = normalizePath(path);
    if (!path) path = sectionPath("latest");
    const seq = ++state.seq;
    state.path = path;
    state.next = null;
    moreEl.hidden = true;
    emptyEl.hidden = true;
    showSkeleton();
    const label = labelForPath(path);
    showState("loading", label ? `Loading ${label}…` : "Loading…");
    setStatus("LOADING");
    highlightNav(path);
    // Sources are external: tell the user when a cold load is slow instead of
    // leaving an unchanging spinner.
    const slow = setTimeout(() => {
      if (seq === state.seq) {
        showState("loading", "Still loading — the sources are slow to answer today.");
      }
    }, 10000);

    try {
      const data = await api(path);
      if (seq !== state.seq) return;
      hideState();
      if (navigator.onLine) hideBanner();
      state.items = Array.isArray(data.items)
        ? data.items.filter((item) => item && typeof item === "object")
        : [];
      state.next = typeof data.next === "string" ? data.next : null;
      state.title = typeof data.title === "string" ? data.title : "";
      titleEl.textContent = state.title || "Rave Player";
      render(state.items);
      moreEl.hidden = !state.next;
      setStatus(`${state.items.length} ITEMS`);
      if (path.startsWith("/api/search")) {
        const query = new URLSearchParams(path.split("?")[1] || "").get("q") || "";
        if (query) searchInput.value = query;
        if (!query) emptyEl.textContent = SEARCH_EMPTY;
      }
      if (push) pushHistory(path);
      if (!state.current) {
        document.title = state.title ? `${state.title} — Rave Player` : "Rave Player";
      }
      if (typeof options.scrollY === "number") window.scrollTo(0, options.scrollY);
      else scrollTop();
    } catch (error) {
      if (seq !== state.seq) return;
      render([]);
      emptyEl.hidden = true;
      setStatus("ERROR");
      const text = describeError(error);
      showState("error", text, () => navigate(path, { push: false }));
      if (error.offline) showBanner("You're offline — browsing the last loaded list still works, playback needs a connection.");
    } finally {
      clearTimeout(slow);
    }
  }

  async function loadMore() {
    if (!state.next || loadMore.pending) return;
    const button = $("more-btn");
    const seq = state.seq;
    const next = state.next;
    loadMore.pending = true;
    button.disabled = true;
    button.textContent = "Loading…";
    try {
      const data = await api(next);
      if (seq !== state.seq || state.next !== next) return;
      const fresh = (Array.isArray(data.items) ? data.items : [])
        .filter((item) => item && typeof item === "object");
      const known = new Set(state.items.map(itemKey));
      const added = fresh.filter((item) => {
        const key = itemKey(item);
        if (known.has(key)) return false;
        known.add(key);
        return true;
      });
      state.items = state.items.concat(added);
      state.next = typeof data.next === "string" ? data.next : null;
      if (state.queueSource === state.path && state.queue.length) {
        const queued = new Set(state.queue.map(itemKey));
        added.filter(playable).forEach((item) => {
          if (!queued.has(itemKey(item))) { state.queue.push(item); queued.add(itemKey(item)); }
        });
        updateQueueCount();
        if (!queueEl.hidden) renderQueue();
      }
      // Append rather than re-render: rebuilding every row steals the user's
      // scroll position and re-decodes images on a phone.
      if (added.length) {
        const fragment = document.createDocumentFragment();
        added.forEach((item) => fragment.appendChild(row(item)));
        listEl.appendChild(fragment);
      }
      listEl.setAttribute("aria-busy", "false");
      emptyEl.hidden = true;
      paintNowPlaying();
      moreEl.hidden = !state.next;
      setStatus(`${state.items.length} ITEMS`);
    } catch (error) {
      toast(`Could not load more: ${messageFor(error)}`, 4000);
    } finally {
      loadMore.pending = false;
      button.disabled = false;
      button.textContent = "Load more";
      moreEl.hidden = !state.next;
    }
  }

  /* ------------------------------------------------------------ player */

  function queueFrom(items) {
    return items.filter(playable);
  }

  function updateQueueCount() {
    const total = state.queue.length;
    const position = total && state.index >= 0 ? state.index + 1 : 0;
    $("p-queue").textContent = `${position}/${total}`;
    $("p-queue").setAttribute("aria-label",
      `Show play queue (${position} of ${total})`);
  }

  function setPlayerStatus(text) {
    const el = $("p-status");
    el.textContent = text || "";
    el.hidden = !text;
  }

  function setPlayIcon(playing) {
    setIcon($("p-play"), playing ? "pause" : "play");
    $("p-play").setAttribute("aria-label", playing ? "Pause" : "Play");
  }

  async function resolveAndPlay(item, index) {
    try {
      const data = await api(item.play_via);
      let url = typeof data.url === "string" ? data.url : "";
      if (!url && Array.isArray(data.items)) {
        // /api/filter/<key>/live resolves to a station *list*, not {url}; the
        // "live now" row still has to play the first station.
        const first = data.items.find((entry) => entry && typeof entry.play === "string" && entry.play);
        if (first) url = first.play;
      }
      if (!url) throw new Error("no stream");
      // This runs while Safari's ~5 s transient activation is still valid, so
      // playItem()'s audio.play() is treated as gesture-started. If resolving
      // took longer, the player says "Tap play" instead of failing silently.
      playItem(item, url, index);
    } catch (error) {
      setPlayerStatus(`Could not resolve that stream (${messageFor(error)})`);
      toast(`Could not resolve that stream: ${messageFor(error)}`, 4000);
    }
  }

  function playItem(item, url, index) {
    if (index == null || index < 0) {
      if (!state.queue.length || state.queueSource !== state.path) {
        state.queue = queueFrom(state.items);
        state.queueSource = state.path;
      }
      if (!state.queue.length) {
        state.queue = [item];
        state.queueSource = state.path;
      }
      const key = itemKey(item);
      const found = state.queue.findIndex((entry) => itemKey(entry) === key);
      index = found >= 0 ? found : 0;
    }
    if (!state.queue.length) { state.queue = [item]; state.queueSource = state.path; }
    state.index = index;
    state.current = { item, url };
    state.currentKey = itemKey(item);

    setPlayerStatus("");
    player.hidden = false;
    document.body.classList.add("has-player");
    measurePlayer();
    $("p-title").textContent = item.label || "Untitled";
    $("p-sub").textContent = item.sub || "";
    const art = item.art || "/static/art/radio.png";
    $("p-art").src = art;
    const on = item.fav ? state.faved.has(favKey(item.fav)) : false;
    setIcon($("p-fav"), "heart");
    $("p-fav").classList.toggle("faved", on);
    $("p-fav").setAttribute("aria-pressed", String(on));
    $("p-fav").setAttribute("aria-label", `${on ? "Remove" : "Add"} current track ${on ? "from" : "to"} favourites`);
    updateQueueCount();
    if (!queueEl.hidden) renderQueue();
    paintNowPlaying();
    document.title = `${item.label || "Rave Player"} — Rave Player`;

    audio.src = `/api/stream?u=${encodeURIComponent(url)}`;
    updatePositionState.last = 0;
    setMediaSession(item);
    refreshAirPlayButton();
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started.catch((error) => {
        if (!error || error.name === "AbortError") return;
        if (error.name === "NotAllowedError") setPlayerStatus("Tap play to start audio");
        else setPlayerStatus("Stream not started — tap play to retry");
        setPlayIcon(false);
        mediaSessionState("paused");
      });
    }
  }

  function playAt(index) {
    const item = state.queue[index];
    if (!item) return;
    if (item.play_via) {
      // Do not move the queue position until the resolver hands back a URL;
      // a failed resolve should leave the previous track selected.
      resolveAndPlay(item, index);
      return;
    }
    playItem(item, item.play, index);
  }

  function step(delta) {
    if (!state.queue.length) {
      toast("Nothing queued yet — pick a track first");
      return;
    }
    playAt((state.index + delta + state.queue.length) % state.queue.length);
  }

  function togglePlay() {
    if (!state.current) {
      const queue = queueFrom(state.items);
      if (!queue.length) { toast("Pick a track to start"); return; }
      state.queue = queue;
      state.queueSource = state.path;
      state.index = 0;
      playAt(0);
      return;
    }
    if (audio.error) { retryPlayback(); return; }
    if (audio.paused) {
      const started = audio.play();
      if (started && typeof started.catch === "function") {
        started.catch((error) => {
          if (error && error.name === "AbortError") return;
          setPlayerStatus("Tap play to start audio");
        });
      }
    } else {
      audio.pause();
    }
  }

  function retryPlayback() {
    if (!state.current) return;
    setPlayerStatus("Retrying…");
    audio.src = `/api/stream?u=${encodeURIComponent(state.current.url)}`;
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started.catch((error) => {
        if (error && error.name === "AbortError") return;
        setPlayerStatus("Stream unavailable — press play to retry");
      });
    }
  }

  function seekBy(seconds) {
    if (!state.current) return;
    if (!isFinite(audio.duration) || audio.duration <= 0) {
      toast("This is a live stream — nothing to seek");
      return;
    }
    audio.currentTime = Math.max(0, Math.min(audio.duration - 0.25, audio.currentTime + seconds));
    updatePositionState(true);
  }

  function closePlayer() {
    state.current = null;
    state.currentKey = "";
    state.index = -1;
    state.queue = [];
    state.queueSource = "";
    try { audio.pause(); } catch (_) { /* ignore */ }
    audio.removeAttribute("src");
    try { audio.load(); } catch (_) { /* ignore */ }
    player.hidden = true;
    document.body.classList.remove("has-player");
    document.body.style.removeProperty("--player");
    setPlayerStatus("");
    $("p-title").textContent = "—";
    $("p-sub").textContent = "";
    $("p-time").textContent = "0:00";
    $("p-dur").textContent = "0:00";
    rangeEl.value = "0";
    rangeEl.disabled = false;
    setPlayIcon(false);
    updateQueueCount();
    toggleQueue(false);
    clearMediaSession();
    refreshAirPlayButton();
    document.title = state.title ? `${state.title} — Rave Player` : "Rave Player";
    paintNowPlaying();
  }

  /* ------------------------------------------------------------ media session */

  // Actions Safari/Chrome do not support throw from setActionHandler; every
  // call is guarded. Passing null clears an action, which is how the skip
  // buttons are removed for live streams once the duration proves infinite.
  const MEDIA_ACTIONS = ["play", "pause", "stop", "previoustrack", "nexttrack",
                         "seekbackward", "seekforward", "seekto"];

  function mediaPlay() {
    const started = audio.play();
    if (started && typeof started.catch === "function") started.catch(() => {});
  }

  function mediaPause() {
    audio.pause();
  }

  function mediaSeekBack(details) {
    seekBy(-((details && details.seekOffset) || 10));
  }

  function mediaSeekForward(details) {
    seekBy((details && details.seekOffset) || 10);
  }

  function mediaSeekTo(details) {
    if (!details || !isFinite(details.seekTime)) return;
    const duration = audio.duration;
    if (!isFinite(duration) || duration <= 0) return;
    const target = Math.max(0, Math.min(details.seekTime, duration - 0.25));
    try {
      if (details.fastSeek && typeof audio.fastSeek === "function") audio.fastSeek(target);
      else audio.currentTime = target;
    } catch (_) { /* ignore */ }
    updatePositionState(true);
  }

  function registerMediaSessionActions() {
    if (!MEDIA_SESSION) return;
    setAction("play", mediaPlay);
    setAction("pause", mediaPause);
    setAction("previoustrack", () => step(-1));
    setAction("nexttrack", () => step(1));
    setAction("stop", () => closePlayer());
    updateMediaSessionSeekActions();
  }

  function updateMediaSessionSeekActions() {
    if (!MEDIA_SESSION) return;
    // Lock-screen skip controls only make sense for finite media. Live radio
    // has duration Infinity; leaving the handlers registered would show skip
    // buttons that can only pop a toast.
    const seekable = isFinite(audio.duration) && audio.duration > 0;
    setAction("seekbackward", seekable ? mediaSeekBack : null);
    setAction("seekforward", seekable ? mediaSeekForward : null);
    setAction("seekto", seekable ? mediaSeekTo : null);
  }

  function setMediaSession(item) {
    if (!MEDIA_SESSION) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: item.label || "Rave Player",
        artist: item.sub || "Rave Player",
        album: state.title || "Rave Player",
        artwork: [{ src: absolutize(item.art || "/static/art/radio.png") }],
      });
    } catch (_) { /* ignore */ }
    registerMediaSessionActions();
    mediaSessionState(audio.paused ? "paused" : "playing");
    // Do not seed the position here: duration can still belong to the previous
    // track until loadedmetadata lands. The metadata event sets it properly.
  }

  function setAction(name, handler) {
    if (!MEDIA_SESSION) return;
    try { navigator.mediaSession.setActionHandler(name, handler); } catch (_) { /* unsupported */ }
  }

  function clearMediaSession() {
    if (!MEDIA_SESSION) return;
    MEDIA_ACTIONS.forEach((name) => setAction(name, null));
    try { navigator.mediaSession.metadata = null; } catch (_) { /* ignore */ }
    mediaSessionState("none");
    try {
      if (typeof navigator.mediaSession.setPositionState === "function") {
        navigator.mediaSession.setPositionState(null);
      }
    } catch (_) { /* ignore */ }
  }

  function mediaSessionState(value) {
    if (!MEDIA_SESSION) return;
    try { navigator.mediaSession.playbackState = value; } catch (_) { /* ignore */ }
  }

  function updatePositionState(force) {
    if (!MEDIA_SESSION || typeof navigator.mediaSession.setPositionState !== "function") return;
    const duration = audio.duration;
    if (!isFinite(duration) || duration <= 0) {
      if (force) {
        try { navigator.mediaSession.setPositionState(null); } catch (_) { /* ignore */ }
      }
      return;
    }
    const now = Date.now();
    if (!force && now - (updatePositionState.last || 0) < 900) return;
    updatePositionState.last = now;
    try {
      navigator.mediaSession.setPositionState({
        duration,
        playbackRate: 1,
        position: Math.min(Math.max(audio.currentTime, 0), duration),
      });
    } catch (_) { /* ignore */ }
  }

  // iOS can drop media events while the page is hidden or in the back/forward
  // cache; re-read the element rather than trusting the last painted state.
  function resyncPlaybackUi() {
    if (!state.current) return;
    setPlayIcon(!audio.paused);
    updateSeekDisplay();
    paintNowPlaying();
    if (MEDIA_SESSION) {
      registerMediaSessionActions();
      mediaSessionState(audio.paused ? "paused" : "playing");
      updatePositionState(true);
    }
    refreshAirPlayButton();
  }

  /* ------------------------------------------------------------ favourites */

  function syncFavButtons(key) {
    const on = state.faved.has(key);
    document.querySelectorAll(".icon-btn[data-fav-key]").forEach((button) => {
      if (button.dataset.favKey !== key) return;
      setIcon(button, "heart");
      button.classList.toggle("faved", on);
      button.setAttribute("aria-pressed", String(on));
      button.title = on ? "Remove from favourites" : "Add to favourites";
      const name = button.closest(".row") ? button.closest(".row").dataset.label : "track";
      button.setAttribute("aria-label", `${on ? "Remove" : "Add"} ${name} ${on ? "from" : "to"} favourites`);
    });
    const currentFav = state.current && state.current.item && state.current.item.fav;
    if (currentFav && favKey(currentFav) === key) {
      setIcon($("p-fav"), "heart");
      $("p-fav").classList.toggle("faved", on);
      $("p-fav").setAttribute("aria-pressed", String(on));
      $("p-fav").setAttribute("aria-label", `${on ? "Remove" : "Add"} current track ${on ? "from" : "to"} favourites`);
    }
  }

  async function toggleFav(fav, button) {
    const key = favKey(fav);
    const on = state.faved.has(key);
    if (button) button.disabled = true;
    try {
      if (on) {
        const data = await api(`/api/favourites/${encodeURIComponent(key)}`, { method: "DELETE" });
        if (data.removed) {
          state.faved.delete(key);
          syncFavButtons(key);
          toast("Removed from favourites");
        } else {
          toast("Could not remove that favourite");
        }
      } else {
        const data = await api("/api/favourites", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(fav),
        });
        if (data.saved) {
          state.faved.add(key);
          syncFavButtons(key);
          toast("Added to favourites");
        } else {
          toast("Could not save that favourite — is the data folder writable?", 4000);
        }
      }
    } catch (error) {
      toast(`Favourite failed: ${messageFor(error)}`, 4000);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function loadFavedKeys() {
    try {
      const data = await api("/api/favourites", { timeout: 12000 });
      state.faved = new Set((data.items || [])
        .filter((item) => item && item.fav_key)
        .map((item) => item.fav_key));
    } catch (_) {
      /* favourites are optional; the rest of the app still works */
    }
  }

  /* ------------------------------------------------------------ queue panel */

  function toggleQueue(force) {
    const open = typeof force === "boolean" ? force : queueEl.hidden;
    queueEl.hidden = !open;
    $("p-queue").setAttribute("aria-expanded", String(open));
    if (open) {
      renderQueue();
      $("queue-close").focus();
    }
  }

  function renderQueue() {
    const list = $("queue-list");
    const fragment = document.createDocumentFragment();
    state.queue.forEach((item, index) => {
      const entry = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "queue-item";
      if (index === state.index) button.setAttribute("aria-current", "true");
      const number = document.createElement("span");
      number.className = "q-index";
      number.textContent = String(index + 1);
      const label = document.createElement("span");
      label.className = "q-label";
      label.textContent = item.label || "Untitled";
      button.append(number, label);
      button.addEventListener("click", () => playAt(index));
      entry.appendChild(button);
      fragment.appendChild(entry);
    });
    list.textContent = "";
    list.appendChild(fragment);
    $("queue-count").textContent = state.queue.length
      ? `${Math.max(state.index + 1, 0)} / ${state.queue.length}`
      : "empty";
    const current = list.querySelector('[aria-current="true"]');
    if (current && typeof current.scrollIntoView === "function") {
      current.scrollIntoView({ block: "nearest" });
    }
  }

  /* ------------------------------------------------------------ help */

  function toggleHelp(force) {
    const open = typeof force === "boolean" ? force : helpEl.hidden;
    helpEl.hidden = !open;
    if (open) {
      // Focus the card, not the Close button: on a small screen focusing the
      // button scrolls the card past the touch instructions.
      const card = helpEl.querySelector(".help-card");
      if (card && typeof card.focus === "function") {
        try { card.focus({ preventScroll: true }); } catch (_) { card.focus(); }
      }
    } else {
      $("help-btn").focus();
    }
  }

  function trapFocus(event) {
    const focusables = helpEl.querySelectorAll("button, [href], [tabindex]:not([tabindex='-1'])");
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === helpEl.querySelector(".help-card"))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /* ------------------------------------------------------------ history */

  function onHistory() {
    const stored = history.state && typeof history.state.path === "string"
      ? normalizePath(history.state.path) : "";
    const path = stored || pathFromHash() || sectionPath("latest");
    state.depth = Number((history.state && history.state.depth) || 0) || 0;
    $("back").hidden = state.depth <= 0;
    const scrollY = history.state && Number.isFinite(history.state.scrollY)
      ? history.state.scrollY : 0;
    if (path === state.path) {
      window.scrollTo(0, scrollY);
      return;
    }
    navigate(path, { push: false, scrollY });
  }

  /* ------------------------------------------------------------ events */

  function focusSearch() {
    searchInput.focus();
    searchInput.select();
  }

  function isTyping(target) {
    return target instanceof Element
      && !!target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']");
  }

  function isInteractive(target) {
    return target instanceof Element
      && !!target.closest("button, a, [role='button'], input, select, textarea");
  }

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented) return;

    if (!helpEl.hidden) {
      if (event.key === "Escape") { event.preventDefault(); toggleHelp(false); }
      else if (event.key === "Tab") trapFocus(event);
      return;
    }

    const target = event.target;
    if (event.key === "/" && !isTyping(target) && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      focusSearch();
      return;
    }
    if (isTyping(target)) {
      if (event.key === "Escape" && target === searchInput) target.blur();
      return;
    }
    if (event.key === "Escape") {
      if (!queueEl.hidden) { event.preventDefault(); toggleQueue(false); }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "?") { event.preventDefault(); toggleHelp(true); return; }
    if (isInteractive(target)) return; // buttons and rows handle their own Space/Enter

    switch (event.key) {
      case " ":
      case "Spacebar":
        event.preventDefault();
        togglePlay();
        break;
      case "ArrowLeft":
        event.preventDefault();
        seekBy(event.shiftKey ? -30 : -5);
        break;
      case "ArrowRight":
        event.preventDefault();
        seekBy(event.shiftKey ? 30 : 5);
        break;
      case "n":
        step(1);
        break;
      case "p":
        step(-1);
        break;
      case "q":
        toggleQueue();
        break;
      default:
        break;
    }
  });

  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = searchInput.value.trim();
    if (!query) {
      toast("Type something to search");
      searchInput.focus();
      return;
    }
    // A pasted Mixcloud/SoundCloud set URL is not a text query: open the
    // provider's own player panel instead of sending it to the Archive search.
    const embed = externalEmbed(query);
    if (embed) {
      openEmbed(embed);
      return;
    }
    navigate(`/api/search?q=${encodeURIComponent(query)}`);
  });

  $("back").addEventListener("click", () => {
    if ((Number(history.state && history.state.depth) || 0) > 0) history.back();
    else navigate(sectionPath("latest"), { push: false });
  });

  moreEl.addEventListener("click", loadMore);

  $("p-play").addEventListener("click", togglePlay);
  $("p-prev").addEventListener("click", () => step(-1));
  $("p-next").addEventListener("click", () => step(1));
  $("p-close").addEventListener("click", closePlayer);
  $("embed-close").addEventListener("click", closeEmbed);
  $("embed-frame").addEventListener("load", () => {
    if ($("embed-frame").getAttribute("src")) markEmbedLoaded();
  });
  $("embed-panel").addEventListener("click", (event) => {
    if (event.target === $("embed-panel")) closeEmbed();
  });
  $("p-queue").addEventListener("click", () => toggleQueue());
  $("queue-close").addEventListener("click", () => { toggleQueue(false); $("p-queue").focus(); });
  $("help-btn").addEventListener("click", () => toggleHelp());
  $("help-close").addEventListener("click", () => toggleHelp(false));
  helpEl.addEventListener("click", (event) => { if (event.target === helpEl) toggleHelp(false); });

  $("p-fav").addEventListener("click", () => {
    const item = state.current && state.current.item;
    if (!item || !item.fav) { toast("Nothing to favourite here"); return; }
    toggleFav(item.fav, $("p-fav"));
  });

  document.addEventListener("pointerdown", (event) => {
    if (queueEl.hidden) return;
    if (queueEl.contains(event.target) || $("p-queue").contains(event.target)) return;
    toggleQueue(false);
  });

  let rangeActive = false;
  let rangeCommitTimer = 0;

  function seekFromSlider() {
    const duration = audio.duration;
    if (!isFinite(duration) || duration <= 0) return;
    audio.currentTime = (Number(rangeEl.value) / 100) * duration;
    updatePositionState(true);
  }

  function queueSliderCommit() {
    clearTimeout(rangeCommitTimer);
    // Native range inputs normally fire change on release; this is the
    // fallback for the iOS cases where they don't.
    rangeCommitTimer = setTimeout(() => {
      if (rangeActive) seekFromSlider();
      rangeActive = false;
    }, 250);
  }

  rangeEl.addEventListener("pointerdown", () => { rangeActive = true; });
  rangeEl.addEventListener("pointerup", queueSliderCommit);
  rangeEl.addEventListener("pointercancel", () => { rangeActive = false; });
  // Older iOS: if pointer events are missing, touch end still commits.
  rangeEl.addEventListener("touchend", queueSliderCommit, { passive: true });
  rangeEl.addEventListener("touchcancel", () => { rangeActive = false; }, { passive: true });
  rangeEl.addEventListener("blur", () => { rangeActive = false; });
  rangeEl.addEventListener("input", () => {
    // While the thumb is held, paint the time only. Seeking on every input
    // event queues a Range request per pixel on iOS and fights timeupdate.
    rangeActive = true;
    const duration = audio.duration;
    if (!isFinite(duration) || duration <= 0) return;
    const seconds = (Number(rangeEl.value) / 100) * duration;
    $("p-time").textContent = fmtTime(seconds);
    rangeEl.setAttribute("aria-valuetext", `${fmtTime(seconds)} of ${fmtTime(duration)}`);
  });
  rangeEl.addEventListener("change", () => {
    clearTimeout(rangeCommitTimer);
    seekFromSlider();
    rangeActive = false;
  });

  audio.addEventListener("play", () => {
    setPlayIcon(true);
    mediaSessionState("playing");
    setPlayerStatus("");
    paintNowPlaying();
    updatePositionState(true);
    refreshAirPlayButton();
  });
  audio.addEventListener("pause", () => {
    if (!state.current) return;
    setPlayIcon(false);
    mediaSessionState("paused");
    paintNowPlaying();
    updatePositionState(true);
  });
  audio.addEventListener("waiting", () => { if (state.current) setPlayerStatus("Buffering…"); });
  audio.addEventListener("playing", () => {
    setPlayerStatus("");
    setPlayIcon(true);
    updateMediaSessionSeekActions();
    refreshAirPlayButton();
  });
  audio.addEventListener("canplay", () => { if (state.current) setPlayerStatus(""); });
  audio.addEventListener("loadedmetadata", () => {
    updateSeekDisplay();
    updateMediaSessionSeekActions();
    updatePositionState(true);
    refreshAirPlayButton();
  });
  audio.addEventListener("durationchange", () => {
    updateSeekDisplay();
    updateMediaSessionSeekActions();
    updatePositionState(true);
  });
  audio.addEventListener("seeked", () => updatePositionState(true));
  audio.addEventListener("ended", () => {
    if (!state.current) return;
    if (state.queue.length > 1) step(1);
    else {
      setPlayIcon(false);
      mediaSessionState("paused");
      updateMediaSessionSeekActions();
    }
  });
  audio.addEventListener("error", () => {
    if (!state.current) return;
    const code = audio.error && audio.error.code;
    let message = code === 4 ? "This stream can't be played here"
      : code === 2 ? "Network error — stream unavailable"
      : "Playback failed";
    if (airplayIsWireless()) {
      message = "The AirPlay device couldn't load this stream";
      toast("AirPlay device couldn't load the stream — it must be able to reach this server (same Wi-Fi, no client isolation).", 6500);
    } else {
      toast(message, 4000);
    }
    setPlayerStatus(`${message} — tap play to retry`);
    setPlayIcon(false);
    mediaSessionState("paused");
    paintNowPlaying();
  });
  audio.addEventListener("timeupdate", () => {
    if (!rangeActive) updateSeekDisplay();
    updatePositionState();
  });

  function updateSeekDisplay() {
    const duration = audio.duration;
    const live = !isFinite(duration) || duration <= 0;
    $("p-time").textContent = fmtTime(audio.currentTime);
    $("p-dur").textContent = live ? "LIVE" : fmtTime(duration);
    rangeEl.disabled = live;
    if (!live && !rangeActive) rangeEl.value = String((audio.currentTime / duration) * 100);
    rangeEl.setAttribute("aria-valuetext", live
      ? "Live stream"
      : `${fmtTime(audio.currentTime)} of ${fmtTime(duration)}`);
  }

  window.addEventListener("popstate", onHistory);
  window.addEventListener("hashchange", () => {
    const path = pathFromHash();
    if (path && path !== state.path) navigate(path, { push: false });
  });

  window.addEventListener("offline", () => {
    showBanner("You're offline. The current list stays usable; playback needs a connection.");
  });
  window.addEventListener("online", () => {
    hideBanner();
    toast("Back online");
  });

  document.addEventListener("visibilitychange", () => {
    // Never pause here: backgrounding or locking the phone is how a player is
    // meant to work. WebKit throttles events while hidden, so re-read the
    // element when the page comes back instead of trusting the last paint.
    if (document.visibilityState === "visible") resyncPlaybackUi();
    else updatePositionState(true);
  });

  window.addEventListener("pageshow", (event) => {
    if (event.persisted) resyncPlaybackUi();
  });

  window.addEventListener("pagehide", (event) => {
    // Do not pause: pagehide also fires when iOS moves the page into the
    // back/forward cache after backgrounding, and pausing would kill
    // background audio. A real unload tears the element down anyway.
    if (!event.persisted) updatePositionState(true);
  });

  /* ------------------------------------------------------------ boot */

  function measureTopbar() {
    const topbar = document.querySelector(".topbar");
    if (!topbar) return;
    const apply = () => {
      const height = Math.round(topbar.getBoundingClientRect().height);
      if (height > 0) document.documentElement.style.setProperty("--topbar-h", `${height}px`);
    };
    apply();
    if ("ResizeObserver" in window) new ResizeObserver(apply).observe(topbar);
    window.addEventListener("resize", apply, { passive: true });
  }

  // The player height changes with the safe-area inset and with the number of
  // wrapped rows, so the stage padding must follow the measured height rather
  // than a hard-coded 92/150 px.
  function measurePlayer() {
    if (player.hidden) {
      document.body.style.removeProperty("--player");
      return;
    }
    const height = Math.ceil(player.getBoundingClientRect().height);
    if (height > 0 && document.body.style.getPropertyValue("--player") !== `${height}px`) {
      document.body.style.setProperty("--player", `${height}px`);
    }
  }

  function setupPlayerSizing() {
    measurePlayer();
    if ("ResizeObserver" in window) new ResizeObserver(measurePlayer).observe(player);
    window.addEventListener("resize", measurePlayer, { passive: true });
    window.addEventListener("orientationchange", measurePlayer, { passive: true });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", measurePlayer, { passive: true });
    }
  }

  async function boot() {
    measureTopbar();
    setupPlayerSizing();
    setupRemotePlayback();
    try { history.scrollRestoration = "manual"; } catch (_) { /* ignore */ }
    if (!navigator.onLine) {
      showBanner("You're offline. The current list stays usable; playback needs a connection.");
    }
    try {
      const menu = await api("/api/menu", { timeout: 15000 });
      navEl.textContent = "";
      (menu.sections || []).forEach((section) => {
        if (!section || !section.id) return;
        const button = document.createElement("button");
        button.type = "button";
        const icon = document.createElement("img");
        icon.src = section.icon || "/static/art/tapepacks.png";
        icon.alt = "";
        icon.width = 22;
        icon.height = 22;
        const label = document.createElement("span");
        label.textContent = section.label || section.id;
        button.append(icon, label);
        button.dataset.path = sectionPath(section.id);
        if (section.sub) button.title = section.sub;
        button.addEventListener("click", () => navigate(sectionPath(section.id)));
        navEl.appendChild(button);
      });
    } catch (error) {
      navEl.textContent = "";
      const message = document.createElement("span");
      message.className = "rail-loading";
      message.textContent = "Could not load sections";
      navEl.appendChild(message);
      showBanner(`Could not load the section menu (${messageFor(error)}).`);
    }

    await loadFavedKeys();
    const initial = pathFromHash() || sectionPath("latest");
    const initialScroll = Math.max(0, Math.round(window.scrollY));
    state.depth = 0;
    $("back").hidden = true;
    try {
      history.replaceState({ path: initial, depth: 0, scrollY: initialScroll }, "", "#" + initial);
    } catch (_) { /* ignore */ }
    await navigate(initial, { push: false, scrollY: initialScroll });
  }

  boot();
})();
