/* Unit tests for static/embed.js (run by test/test_embed_dock.py via Node).
 *
 * The provider APIs are cross-origin postMessage protocols. These checks pin
 * the two security-critical parts (exact source/origin matching and the
 * message grammar), plus the command handshake used by the dock.
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const Embed = require(path.join(__dirname, "..", "..", "static", "embed.js"));

let passed = 0;

function test(name, fn) {
  fn();
  passed += 1;
  console.log("ok " + passed + " - " + name);
}

/* ------------------------------------------------------------------ grammar */

test("recognises only the two provider keys", () => {
  assert.strictEqual(Embed.providerLabel("soundcloud"), "SoundCloud");
  assert.strictEqual(Embed.providerLabel("mixcloud"), "Mixcloud");
  assert.strictEqual(Embed.providerLabel("youtube"), "Provider");
  assert.strictEqual(Embed.providerLabel(""), "Provider");
});

test("allows the exact SoundCloud widget origin only", () => {
  assert.ok(Embed.originAllowed("soundcloud", "https://w.soundcloud.com"));
  assert.ok(Embed.originAllowed("soundcloud", "https://w.soundcloud.com/"));
  for (const origin of [
    "http://w.soundcloud.com",
    "https://w.soundcloud.com.evil.test",
    "https://evil.test",
    "https://w.soundcloud.com:8443",
    "",
  ]) {
    assert.strictEqual(Embed.originAllowed("soundcloud", origin), false, origin);
  }
});

test("allows both Mixcloud hosts (the widget 301s to player-widget)", () => {
  assert.ok(Embed.originAllowed("mixcloud", "https://www.mixcloud.com"));
  assert.ok(Embed.originAllowed("mixcloud", "https://player-widget.mixcloud.com"));
  assert.strictEqual(Embed.originAllowed("mixcloud", "http://player-widget.mixcloud.com"), false);
  assert.strictEqual(Embed.originAllowed("mixcloud", "https://player-widget.mixcloud.com.evil.test"), false);
  assert.strictEqual(Embed.originAllowed("mixcloud", "https://soundcloud.com"), false);
});

test("parses SoundCloud play/pause/finish events", () => {
  assert.deepStrictEqual(Embed.parseWidgetMessage("soundcloud", '{"method":"play"}'), {
    provider: "soundcloud", type: "play", value: undefined,
  });
  assert.strictEqual(
    Embed.parseWidgetMessage("soundcloud", '{"method":"pause"}').type, "pause");
  assert.strictEqual(
    Embed.parseWidgetMessage("soundcloud", '{"method":"finish"}').type, "finish");
  assert.strictEqual(
    Embed.parseWidgetMessage("soundcloud", '{"method":"ready"}').type, "ready");
});

test("rejects malformed or foreign SoundCloud messages", () => {
  for (const data of [
    "not json",
    "null",
    "[]",
    '{"method":42}',
    '{"mixcloud":"playerWidget","type":"play"}',
    "",
    null,
  ]) {
    assert.strictEqual(Embed.parseWidgetMessage("soundcloud", data), null, String(data));
  }
});

test("parses the Mixcloud ready/getApi event envelope", () => {
  assert.strictEqual(
    Embed.parseWidgetMessage("mixcloud", '{"mixcloud":"playerWidget","type":"ready"}').type,
    "ready");
  assert.strictEqual(
    Embed.parseWidgetMessage("mixcloud", '{"mixcloud":"playerWidget","type":"api","data":{"methods":[]}}').type,
    "api");
  const play = Embed.parseWidgetMessage(
    "mixcloud",
    '{"mixcloud":"playerWidget","type":"event","data":{"eventName":"play","args":[]}}');
  assert.deepStrictEqual(play, {
    provider: "mixcloud", type: "play", value: [],
  });
});

test("rejects bare SoundCloud-shaped data sent to the Mixcloud bridge", () => {
  assert.strictEqual(Embed.parseWidgetMessage("mixcloud", '{"method":"play"}'), null);
  assert.strictEqual(Embed.parseWidgetMessage("mixcloud", '{"mixcloud":"playerWidget"}'), null);
  assert.strictEqual(Embed.parseWidgetMessage("mixcloud", '{"mixcloud":"playerWidget","type":"event"}'), null);
});

test("maps provider events to play/pause/ended/error", () => {
  assert.strictEqual(Embed.eventKind("soundcloud", "play"), "play");
  assert.strictEqual(Embed.eventKind("soundcloud", "pause"), "pause");
  assert.strictEqual(Embed.eventKind("soundcloud", "finish"), "ended");
  assert.strictEqual(Embed.eventKind("soundcloud", "error"), "error");
  assert.strictEqual(Embed.eventKind("soundcloud", "ready"), "");
  assert.strictEqual(Embed.eventKind("mixcloud", "play"), "play");
  assert.strictEqual(Embed.eventKind("mixcloud", "ended"), "ended");
  assert.strictEqual(Embed.eventKind("mixcloud", "progress"), "");
});

test("commands are provider-specific", () => {
  const soundcloud = Embed.commands("soundcloud");
  assert.deepStrictEqual(soundcloud.map((c) => c.value), ["play", "pause", "finish"]);
  assert.ok(soundcloud.every((c) => c.method === "addEventListener"));
  assert.deepStrictEqual(Embed.commands("mixcloud"), [{ type: "getApi" }]);
  assert.deepStrictEqual(Embed.commands("unknown"), []);
});

/* --------------------------------------------------------------- attachment */

function fakeWindow() {
  const listeners = new Set();
  return {
    listeners,
    addEventListener(type, fn) { if (type === "message") listeners.add(fn); },
    removeEventListener(type, fn) { if (type === "message") listeners.delete(fn); },
    dispatch(event) { for (const fn of [...listeners]) fn(event); },
  };
}

function fakeFrame() {
  const posts = [];
  return {
    posts,
    contentWindow: {
      postMessage(payload, origin) { posts.push({ payload: JSON.parse(payload), origin }); },
    },
  };
}

test("attach syncs a SoundCloud listener handshake on ready", () => {
  const win = fakeWindow();
  const frame = fakeFrame();
  const events = [];
  const bridge = Embed.attach({
    provider: "soundcloud",
    frame,
    window: win,
    onEvent: (kind) => events.push(kind),
  });
  assert.strictEqual(bridge.provider, "soundcloud");
  win.dispatch({
    source: frame.contentWindow,
    origin: "https://w.soundcloud.com",
    data: JSON.stringify({ method: "ready" }),
  });
  assert.deepStrictEqual(frame.posts.map((p) => p.payload.value), ["play", "pause", "finish"]);
  assert.ok(frame.posts.every((p) => p.origin === "https://w.soundcloud.com"));
  win.dispatch({
    source: frame.contentWindow,
    origin: "https://w.soundcloud.com",
    data: JSON.stringify({ method: "play" }),
  });
  win.dispatch({
    source: frame.contentWindow,
    origin: "https://w.soundcloud.com",
    data: JSON.stringify({ method: "pause" }),
  });
  assert.deepStrictEqual(events, ["play", "pause"]);
});

test("ignores the wrong source and the wrong origin", () => {
  const win = fakeWindow();
  const frame = fakeFrame();
  const events = [];
  Embed.attach({
    provider: "soundcloud", frame, window: win,
    onEvent: (kind) => events.push(kind),
  });
  const payload = JSON.stringify({ method: "play" });
  win.dispatch({ source: {}, origin: "https://w.soundcloud.com", data: payload });
  win.dispatch({ source: frame.contentWindow, origin: "https://evil.test", data: payload });
  assert.deepStrictEqual(events, []);
});

test("Mixcloud getApi goes to both allowed hosts, never *", () => {
  const win = fakeWindow();
  const frame = fakeFrame();
  Embed.attach({ provider: "mixcloud", frame, window: win });
  win.dispatch({
    source: frame.contentWindow,
    origin: "https://player-widget.mixcloud.com",
    data: JSON.stringify({ mixcloud: "playerWidget", type: "ready" }),
  });
  // One post per allowed origin; the browser drops the one whose target
  // origin does not match the widget document.
  assert.strictEqual(frame.posts.length, 2);
  assert.ok(frame.posts.every((p) => p.payload.type === "getApi"));
  assert.deepStrictEqual(
    frame.posts.map((p) => p.origin).sort(),
    ["https://player-widget.mixcloud.com", "https://www.mixcloud.com"],
  );
  assert.ok(!frame.posts.some((p) => p.origin === "*"));
});

test("destroy removes the message listener", () => {
  const win = fakeWindow();
  const frame = fakeFrame();
  const events = [];
  const bridge = Embed.attach({
    provider: "mixcloud", frame, window: win,
    onEvent: (kind) => events.push(kind),
  });
  bridge.destroy();
  bridge.destroy();
  assert.strictEqual(win.listeners.size, 0);
  win.dispatch({
    source: frame.contentWindow,
    origin: "https://player-widget.mixcloud.com",
    data: JSON.stringify({
      mixcloud: "playerWidget", type: "event", data: { eventName: "play", args: [] },
    }),
  });
  assert.deepStrictEqual(events, []);
});

console.log("PASS " + passed + " embed.js unit checks");
