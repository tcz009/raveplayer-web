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

  const state = {
    items: [],
    next: null,
    title: "",
    queue: [],
    index: -1,
    current: null,
    faved: new Set(),
  };

  /* ------------------------------------------------------------ utilities */

  function toast(message, ms = 2600) {
    const el = $("toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, ms);
  }

  function status(text) { $("status").textContent = text; }

  function fmtTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
             : `${m}:${String(s).padStart(2, "0")}`;
  }

  /* ------------------------------------------------------------ data */

  async function api(path) {
    status("LOADING");
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function render(items) {
    listEl.innerHTML = "";
    items.forEach((item) => listEl.appendChild(row(item)));
    emptyEl.hidden = items.length > 0;
  }

  function row(item) {
    const el = document.createElement("div");
    el.className = "row";
    el.tabIndex = 0;

    const art = document.createElement("img");
    art.className = "row-art";
    art.loading = "lazy";
    art.src = item.art || "/static/art/tapepacks.png";
    art.alt = "";
    art.addEventListener("error", () => { art.src = "/static/art/tapepacks.png"; });

    const text = document.createElement("div");
    const label = document.createElement("div");
    label.className = "row-label";
    label.textContent = item.label;
    const sub = document.createElement("div");
    sub.className = "row-sub";
    sub.textContent = item.sub || "";
    text.append(label, sub);

    const actions = document.createElement("div");
    actions.className = "row-actions";

    if (item.fav) {
      const heart = document.createElement("button");
      heart.className = "icon-btn" + (state.faved.has(favKey(item.fav)) ? " faved" : "");
      heart.textContent = state.faved.has(favKey(item.fav)) ? "♥" : "♡";
      heart.title = "Add to favourites";
      heart.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleFav(item.fav, heart);
      });
      actions.appendChild(heart);
    }

    if (item.fav_key) {
      const remove = document.createElement("button");
      remove.className = "icon-btn";
      remove.textContent = "✕";
      remove.title = "Remove from favourites";
      remove.addEventListener("click", async (event) => {
        event.stopPropagation();
        await fetch(`/api/favourites/${encodeURIComponent(item.fav_key)}`, { method: "DELETE" });
        el.remove();
        toast("Removed from favourites");
      });
      actions.appendChild(remove);
    }

    const action = document.createElement("button");
    action.className = "icon-btn" + (item.play || item.play_via ? " play" : "");
    action.textContent = item.play || item.play_via ? "▶" : "›";
    action.title = item.play || item.play_via ? "Play" : "Open";

    el.append(art, text, actions);
    actions.appendChild(action);

    const activate = () => {
      if (item.play_via) return resolveAndPlay(item);
      if (item.play) return play(item, item.play);
      if (item.open) return navigate(item.open);
    };
    el.addEventListener("click", activate);
    el.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
    });
    return el;
  }

  /* ------------------------------------------------------------ navigation */

  async function navigate(path, options = {}) {
    try {
      const data = await api(path);
      state.items = data.items || [];
      state.next = data.next || null;
      state.title = data.title || "";
      titleEl.textContent = state.title;
      $("back").hidden = !history.state || !history.state.back;
      render(state.items);
      moreEl.hidden = !state.next;
      status(`${state.items.length} ITEMS`);
      if (options.push !== false) {
        history.pushState({ path, back: true }, "", "#" + encodeURIComponent(path));
      }
      document.querySelector(".stage").scrollTo?.(0, 0);
      window.scrollTo({ top: 0 });
      highlightNav(path);
    } catch (error) {
      status("ERROR");
      toast(`Could not load: ${error.message}`);
    }
  }

  async function loadMore() {
    if (!state.next) return;
    const data = await api(state.next);
    state.items = state.items.concat(data.items || []);
    state.next = data.next || null;
    render(state.items);
    moreEl.hidden = !state.next;
  }

  function highlightNav(path) {
    navEl.querySelectorAll("button").forEach((button) => {
      button.classList.toggle("active", button.dataset.path === path);
    });
  }

  /* ------------------------------------------------------------ player */

  function queueFrom(items) {
    return items.filter((item) => item.play || item.play_via);
  }

  async function resolveAndPlay(item) {
    try {
      const data = await api(item.play_via);
      if (!data.url) throw new Error("no stream");
      play(item, data.url);
    } catch (error) {
      toast(`Could not resolve that stream: ${error.message}`);
    }
  }

  function play(item, url) {
    state.queue = queueFrom(state.items);
    state.index = state.queue.findIndex((entry) => entry === item || entry.label === item.label);
    state.current = { item, url };
    const stream = `/api/stream?u=${encodeURIComponent(url)}`;
    audio.src = stream;
    audio.play().catch(() => toast("Press play to start audio"));
    player.hidden = false;
    $("p-title").textContent = item.label;
    $("p-sub").textContent = item.sub || "";
    $("p-art").src = item.art || "/static/art/radio.png";
    $("p-fav").textContent = item.fav && state.faved.has(favKey(item.fav)) ? "♥" : "♡";
    $("p-queue").textContent = `${state.queue.length} in queue`;
    $("p-play").textContent = "⏸";
    document.title = `${item.label} — Rave Player`;
  }

  function step(delta) {
    if (!state.queue.length) return;
    state.index = (state.index + delta + state.queue.length) % state.queue.length;
    const item = state.queue[state.index];
    if (item.play_via) resolveAndPlay(item);
    else play(item, item.play);
  }

  /* ------------------------------------------------------------ favourites */

  function favKey(fav) { return `${fav.kind}:${fav.target}`; }

  async function toggleFav(fav, button) {
    const key = favKey(fav);
    if (state.faved.has(key)) return;
    const response = await fetch("/api/favourites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fav),
    });
    const data = await response.json();
    if (data.saved) {
      state.faved.add(key);
      if (button) { button.textContent = "♥"; button.classList.add("faved"); }
      toast("Added to favourites");
    } else {
      toast("Could not save that favourite");
    }
  }

  async function loadFavedKeys() {
    try {
      const data = await api("/api/favourites");
      state.faved = new Set((data.items || []).filter((item) => item.fav_key).map((item) => item.fav_key));
    } catch (_) { /* favourites are optional */ }
  }

  /* ------------------------------------------------------------ boot */

  async function boot() {
    try {
      const menu = await api("/api/menu");
      navEl.innerHTML = "";
      menu.sections.forEach((section) => {
        const button = document.createElement("button");
        const icon = document.createElement("img");
        icon.src = section.icon;
        icon.alt = "";
        const label = document.createElement("span");
        label.textContent = section.label;
        button.append(icon, label);
        button.dataset.path = sectionPath(section.id);
        button.addEventListener("click", () => navigate(sectionPath(section.id)));
        navEl.appendChild(button);
      });
    } catch (error) {
      navEl.innerHTML = `<span class="rail-loading">Could not load menu</span>`;
      toast(`Menu failed: ${error.message}`);
    }

    await loadFavedKeys();
    const initial = sectionPath("latest");
    history.replaceState({ path: initial }, "", "#" + encodeURIComponent(initial));
    await navigate(initial, { push: false });
  }

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

  /* ------------------------------------------------------------ events */

  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = $("search-input").value.trim();
    if (query) navigate(`/api/search?q=${encodeURIComponent(query)}`);
  });

  $("back").addEventListener("click", () => history.back());

  moreEl.addEventListener("click", loadMore);

  $("p-play").addEventListener("click", () => {
    if (audio.paused) { audio.play(); $("p-play").textContent = "⏸"; }
    else { audio.pause(); $("p-play").textContent = "▶"; }
  });
  $("p-prev").addEventListener("click", () => step(-1));
  $("p-next").addEventListener("click", () => step(1));
  $("p-close").addEventListener("click", () => {
    audio.pause();
    audio.removeAttribute("src");
    player.hidden = true;
  });
  $("p-fav").addEventListener("click", () => {
    if (!state.current?.item?.fav) return toast("Nothing to favourite here");
    toggleFav(state.current.item.fav, $("p-fav"));
  });

  audio.addEventListener("timeupdate", () => {
    const duration = audio.duration || 0;
    $("p-time").textContent = fmtTime(audio.currentTime);
    $("p-dur").textContent = fmtTime(duration);
    $("p-range").value = duration ? (audio.currentTime / duration) * 100 : 0;
  });
  $("p-range").addEventListener("input", () => {
    if (audio.duration) audio.currentTime = (Number($("p-range").value) / 100) * audio.duration;
  });
  audio.addEventListener("ended", () => step(1));
  audio.addEventListener("error", () => toast("Playback failed for that stream"));

  window.addEventListener("popstate", (event) => {
    const path = event.state?.path;
    if (path) navigate(path, { push: false });
  });

  boot();
})();
