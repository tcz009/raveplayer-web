/* Unit tests for static/theme.js (run by test/test_theme.py via Node).
 *
 * The early boot script is deliberately tiny, but two things must be exact:
 * the stored mode (what the Help switch writes) stays separate from the
 * resolved theme (what prefers-color-scheme says), and the root attribute is
 * set before the stylesheet can paint.
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const memory = new Map();
const events = [];
let mediaMatches = true;
let mediaListener = null;

const root = {
  attrs: {},
  setAttribute(name, value) { this.attrs[name] = String(value); },
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
};

const themeMeta = { attrs: {}, setAttribute(name, value) { this.attrs[name] = String(value); } };
const schemeMeta = { attrs: {}, setAttribute(name, value) { this.attrs[name] = String(value); } };

global.document = {
  documentElement: root,
  querySelector(selector) {
    if (selector.includes("theme-color")) return themeMeta;
    if (selector.includes("color-scheme")) return schemeMeta;
    return null;
  },
};

global.window = {
  localStorage: {
    getItem: (key) => (memory.has(key) ? memory.get(key) : null),
    setItem: (key, value) => { memory.set(key, String(value)); },
    removeItem: (key) => { memory.delete(key); },
  },
  matchMedia: () => ({
    get matches() { return mediaMatches; },
    addEventListener: (type, listener) => {
      if (type === "change") mediaListener = listener;
    },
  }),
  dispatchEvent: (event) => { events.push(event); return true; },
  CustomEvent: class CustomEvent {
    constructor(type, init) { this.type = type; this.detail = init && init.detail; }
  },
};

const themePath = path.join(__dirname, "..", "..", "static", "theme.js");

function load() {
  delete require.cache[require.resolve(themePath)];
  return require(themePath);
}

function reset() {
  memory.clear();
  events.length = 0;
  mediaMatches = true;
  mediaListener = null;
  root.attrs = {};
  themeMeta.attrs = {};
  schemeMeta.attrs = {};
}

let passed = 0;

function test(name, fn) {
  fn();
  passed += 1;
  console.log("ok " + passed + " - " + name);
}

test("dark is the default, applied at boot before the CSS", () => {
  reset();
  load();
  assert.strictEqual(root.attrs["data-theme"], "dark");
  assert.strictEqual(root.attrs["data-theme-mode"], "dark");
  assert.strictEqual(themeMeta.attrs.content, "#0a0a0f");
  assert.strictEqual(schemeMeta.attrs.content, "dark");
  assert.strictEqual(memory.has("raveplayer.theme"), false);
  assert.strictEqual(window.RaveTheme.defaultMode, "dark");
});

test("unknown stored values fall back to dark", () => {
  reset();
  memory.set("raveplayer.theme", "neon");
  load();
  assert.strictEqual(window.RaveTheme.get(), "dark");
  assert.strictEqual(window.RaveTheme.normalize("LIGHT"), "light");
  assert.strictEqual(window.RaveTheme.normalize(null), "dark");
  assert.strictEqual(window.RaveTheme.resolve("light"), "light");
});

test("selecting Light persists the mode, not the resolved theme", () => {
  reset();
  load();
  assert.strictEqual(window.RaveTheme.set("light"), "light");
  assert.strictEqual(memory.get("raveplayer.theme"), "light");
  assert.strictEqual(root.attrs["data-theme"], "light");
  assert.strictEqual(root.attrs["data-theme-mode"], "light");
  assert.strictEqual(themeMeta.attrs.content, "#f2eee3");
  assert.strictEqual(schemeMeta.attrs.content, "light");
  assert.strictEqual(events[events.length - 1].type, "rave:theme");
  assert.deepStrictEqual(events[events.length - 1].detail, { mode: "light", theme: "light" });
});

test("System resolves through prefers-color-scheme and stores 'system'", () => {
  reset();
  mediaMatches = true;
  load();
  assert.strictEqual(window.RaveTheme.set("system"), "light");
  assert.strictEqual(memory.get("raveplayer.theme"), "system");
  assert.strictEqual(root.attrs["data-theme"], "light");
  assert.strictEqual(root.attrs["data-theme-mode"], "system");
  assert.strictEqual(window.RaveTheme.current(), "light");
});

test("System follows the media query, explicit modes do not", () => {
  reset();
  mediaMatches = true;
  load();
  window.RaveTheme.set("system");
  assert.ok(mediaListener, "the system listener must be registered");
  mediaMatches = false;
  mediaListener();
  assert.strictEqual(root.attrs["data-theme"], "dark");
  assert.strictEqual(memory.get("raveplayer.theme"), "system");

  window.RaveTheme.set("dark");
  mediaMatches = true;
  mediaListener();
  assert.strictEqual(root.attrs["data-theme"], "dark", "explicit dark must ignore the OS");
  assert.strictEqual(memory.get("raveplayer.theme"), "dark");
});

test("a reload applies the stored choice before first paint", () => {
  reset();
  memory.set("raveplayer.theme", "light");
  load();
  assert.strictEqual(root.attrs["data-theme"], "light");
  assert.strictEqual(themeMeta.attrs.content, "#f2eee3");

  reset();
  memory.set("raveplayer.theme", "system");
  mediaMatches = false;
  load();
  assert.strictEqual(root.attrs["data-theme-mode"], "system");
  assert.strictEqual(root.attrs["data-theme"], "dark");
});

test("storage failures do not break the page", () => {
  reset();
  const real = global.window.localStorage;
  global.window.localStorage = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
    removeItem() { throw new Error("blocked"); },
  };
  load();
  assert.strictEqual(root.attrs["data-theme"], "dark");
  assert.strictEqual(window.RaveTheme.set("light"), "light");
  assert.strictEqual(root.attrs["data-theme"], "light");
  global.window.localStorage = real;
});

assert.ok(passed >= 7, "expected at least 7 checks");
console.log("PASS " + passed + " theme.js unit checks");
