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

  // Safari exposes AirPlay through a WebKit-only hook; Chromium exposes the
  // Remote Playback API (Cast). Both are optional — the button only appears
  // when the browser says it can actually route audio somewhere.
  function setupRemotePlayback() {
    const button = $("p-airplay");
    if (!button) return;

    if (typeof audio.webkitShowPlaybackTargetPicker === "function") {
      button.hidden = false;
      button.addEventListener("click", () => {
        try {
          audio.webkitShowPlaybackTargetPicker();
        } catch (_) {
          toast("AirPlay is not available right now");
        }
      });
      audio.addEventListener("webkitplaybacktargetavailabilitychanged", (event) => {
        button.hidden = event.availability !== "available";
      });
      return;
    }

    const remote = audio.remote;
    if (remote && typeof remote.prompt === "function") {
      button.hidden = false;
      button.addEventListener("click", () => {
        remote.prompt().catch(() => toast("No AirPlay or Cast devices found"));
      });
      if (typeof remote.addEventListener === "function") {
        remote.addEventListener("connect", () => button.classList.add("connected"));
        remote.addEventListener("disconnect", () => button.classList.remove("connected"));
      }
      return;
    }

    button.hidden = true;
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
    listEl.textContent = "";
    listEl.setAttribute("aria-busy", "false");
    emptyEl.textContent = DEFAULT_EMPTY;
    items.forEach((item) => listEl.appendChild(row(item)));
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
    listEl.querySelectorAll(".row").forEach((el) => {
      const playing = !!state.currentKey && el.dataset.key === state.currentKey;
      el.classList.toggle("playing", playing);
      if (playing) el.setAttribute("aria-current", "true");
      else el.removeAttribute("aria-current");
      const button = el.querySelector(".icon-btn.play");
      if (button) {
        setIcon(button, playing && !audio.paused ? "pause" : "play");
        const word = playing ? (audio.paused ? "Resume" : "Pause") : (el.dataset.action || "Play");
        button.setAttribute("aria-label", `${word} ${el.dataset.label || ""}`.trim());
      }
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
      years: "/api/years",
      legacy: "/api/legacy",
      favourites: "/api/favourites",
      search: "/api/search",
    };
    return map[id] || "/api/latest";
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

  function pushHistory(path) {
    const depth = (Number(history.state && history.state.depth) || 0) + 1;
    state.depth = depth;
    try {
      history.pushState({ path, depth }, "", "#" + path);
    } catch (_) {
      window.location.hash = path;
    }
    $("back").hidden = depth <= 0;
  }

  async function navigate(path, options = {}) {
    const push = options.push !== false;
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
      scrollTop();
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
      const fresh = Array.isArray(data.items) ? data.items : [];
      state.items = state.items.concat(fresh);
      state.next = typeof data.next === "string" ? data.next : null;
      if (state.queueSource === state.path && state.queue.length) {
        const known = new Set(state.queue.map(itemKey));
        fresh.filter(playable).forEach((item) => {
          if (!known.has(itemKey(item))) { state.queue.push(item); known.add(itemKey(item)); }
        });
        updateQueueCount();
        if (!queueEl.hidden) renderQueue();
      }
      render(state.items);
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
    $("p-play").replaceChildren();
    setIcon($("p-play"), playing ? "pause" : "play");
    $("p-play").setAttribute("aria-label", playing ? "Pause" : "Play");
  }

  async function resolveAndPlay(item, index) {
    try {
      const data = await api(item.play_via);
      if (!data.url) throw new Error("no stream");
      playItem(item, data.url, index);
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
    setMediaSession(item);
    document.title = `${item.label || "Rave Player"} — Rave Player`;

    audio.src = `/api/stream?u=${encodeURIComponent(url)}`;
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started.catch((error) => {
        if (!error || error.name === "AbortError") return;
        if (error.name === "NotAllowedError") setPlayerStatus("Press play to start audio");
        else setPlayerStatus("Stream not started — press play to retry");
      });
    }
  }

  function playAt(index) {
    const item = state.queue[index];
    if (!item) return;
    if (item.play_via) {
      state.index = index;
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
          setPlayerStatus("Press play to start audio");
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
    if (MEDIA_SESSION) {
      try { navigator.mediaSession.metadata = null; } catch (_) { /* ignore */ }
      try { navigator.mediaSession.playbackState = "none"; } catch (_) { /* ignore */ }
    }
    document.title = state.title ? `${state.title} — Rave Player` : "Rave Player";
    paintNowPlaying();
  }

  /* ------------------------------------------------------------ media session */

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
    setAction("play", () => { const p = audio.play(); if (p) p.catch(() => {}); });
    setAction("pause", () => audio.pause());
    setAction("previoustrack", () => step(-1));
    setAction("nexttrack", () => step(1));
    setAction("seekbackward", (details) => seekBy(-((details && details.seekOffset) || 10)));
    setAction("seekforward", (details) => seekBy((details && details.seekOffset) || 10));
    setAction("seekto", (details) => {
      if (details && isFinite(details.seekTime) && isFinite(audio.duration)) {
        try { audio.currentTime = details.seekTime; } catch (_) { /* ignore */ }
      }
    });
    setAction("stop", () => closePlayer());
  }

  function setAction(name, handler) {
    if (!MEDIA_SESSION) return;
    try { navigator.mediaSession.setActionHandler(name, handler); } catch (_) { /* unsupported */ }
  }

  function mediaSessionState(value) {
    if (!MEDIA_SESSION) return;
    try { navigator.mediaSession.playbackState = value; } catch (_) { /* ignore */ }
  }

  function updatePositionState() {
    if (!MEDIA_SESSION || typeof navigator.mediaSession.setPositionState !== "function") return;
    const duration = audio.duration;
    if (!isFinite(duration) || duration <= 0) return;
    const now = Date.now();
    if (now - updatePositionState.last < 900) return;
    updatePositionState.last = now;
    try {
      navigator.mediaSession.setPositionState({
        duration,
        playbackRate: 1,
        position: Math.min(Math.max(audio.currentTime, 0), duration),
      });
    } catch (_) { /* ignore */ }
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
    list.textContent = "";
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
      list.appendChild(entry);
    });
    $("queue-count").textContent = state.queue.length
      ? `${Math.max(state.index + 1, 0)} / ${state.queue.length}`
      : "empty";
  }

  /* ------------------------------------------------------------ help */

  function toggleHelp(force) {
    const open = typeof force === "boolean" ? force : helpEl.hidden;
    helpEl.hidden = !open;
    if (open) $("help-close").focus();
    else $("help-btn").focus();
  }

  function trapFocus(event) {
    const focusables = helpEl.querySelectorAll("button, [href], [tabindex]:not([tabindex='-1'])");
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
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
    if (path === state.path) return;
    navigate(path, { push: false });
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
  rangeEl.addEventListener("pointerdown", () => { rangeActive = true; });
  rangeEl.addEventListener("pointerup", () => { rangeActive = false; });
  document.addEventListener("pointerup", () => { rangeActive = false; });
  rangeEl.addEventListener("blur", () => { rangeActive = false; });
  rangeEl.addEventListener("input", () => {
    if (!isFinite(audio.duration) || audio.duration <= 0) return;
    audio.currentTime = (Number(rangeEl.value) / 100) * audio.duration;
  });
  rangeEl.addEventListener("change", () => { rangeActive = false; });

  audio.addEventListener("play", () => {
    setPlayIcon(true);
    mediaSessionState("playing");
    setPlayerStatus("");
    paintNowPlaying();
  });
  audio.addEventListener("pause", () => {
    if (!state.current) return;
    setPlayIcon(false);
    mediaSessionState("paused");
    paintNowPlaying();
  });
  audio.addEventListener("waiting", () => { if (state.current) setPlayerStatus("Buffering…"); });
  audio.addEventListener("playing", () => { setPlayerStatus(""); setPlayIcon(true); });
  audio.addEventListener("canplay", () => { if (state.current) setPlayerStatus(""); });
  audio.addEventListener("ended", () => {
    if (!state.current) return;
    if (state.queue.length > 1) step(1);
    else { setPlayIcon(false); mediaSessionState("paused"); }
  });
  audio.addEventListener("error", () => {
    if (!state.current) return;
    const code = audio.error && audio.error.code;
    const message = code === 4 ? "This stream can't be played here"
      : code === 2 ? "Network error — stream unavailable"
      : "Playback failed";
    setPlayerStatus(`${message} — press play to retry`);
    toast(message, 4000);
    setPlayIcon(false);
    mediaSessionState("paused");
    paintNowPlaying();
  });
  audio.addEventListener("loadedmetadata", updateSeekDisplay);
  audio.addEventListener("durationchange", updateSeekDisplay);
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

  async function boot() {
    measureTopbar();
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
    state.depth = 0;
    $("back").hidden = true;
    try {
      history.replaceState({ path: initial, depth: 0 }, "", "#" + initial);
    } catch (_) { /* ignore */ }
    await navigate(initial, { push: false });
  }

  boot();
})();
