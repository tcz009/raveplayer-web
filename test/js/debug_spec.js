/* Unit tests for static/debug.js (run by test/test_debug.py via Node).
 *
 * Covers the two things that must be right before a debug log is safe to ship:
 * the opt-in decision, and what does (and does not) reach the ring buffer.
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const RaveDebug = require(path.join(__dirname, "..", "..", "static", "debug.js"));

let passed = 0;

function test(name, fn) {
  fn();
  passed += 1;
  console.log("ok " + passed + " - " + name);
}

/* ------------------------------------------------------------ toggle */

test("debug is off by default", () => {
  assert.strictEqual(RaveDebug.resolveEnabled("", null), false);
  assert.strictEqual(RaveDebug.resolveEnabled("?section=latest", "0"), false);
  assert.strictEqual(RaveDebug.enabled, false);
});

test("?debug=1 and the stored flag enable the log", () => {
  assert.strictEqual(RaveDebug.flagFromSearch("?debug=1"), true);
  assert.strictEqual(RaveDebug.flagFromSearch("?x=2&debug=true"), true);
  assert.strictEqual(RaveDebug.flagFromSearch("?debug=0"), false);
  assert.strictEqual(RaveDebug.flagFromSearch(""), null);
  assert.strictEqual(RaveDebug.resolveEnabled("?debug=1", null), true);
  assert.strictEqual(RaveDebug.resolveEnabled("", "1"), true);
  assert.strictEqual(RaveDebug.resolveEnabled("", "true"), true);
  // An explicit ?debug=0 beats a previously stored flag.
  assert.strictEqual(RaveDebug.resolveEnabled("?debug=0", "1"), false);
});

/* ------------------------------------------------------------ redaction */

test("sensitive keys are replaced wholesale", () => {
  const out = RaveDebug.redact({
    password: "hunter2",
    token: "abc123",
    Cookie: "raveplayer_session=deadbeef",
    sessionId: "sess-123",
    authorization: "Bearer abc.def.ghi",
    csrf: "xyz",
    api_key: "k-1",
    user: "owner",
    count: 3,
    ok: true,
    empty: null,
  });
  for (const key of ["password", "token", "Cookie", "sessionId", "authorization", "csrf", "api_key"]) {
    assert.strictEqual(out[key], "[redacted]", key + " should be redacted");
  }
  assert.strictEqual(out.user, "owner");
  assert.strictEqual(out.count, 3);
  assert.strictEqual(out.ok, true);
  assert.strictEqual(out.empty, null);
});

test("tokens in URLs and header-shaped strings are scrubbed", () => {
  const out = RaveDebug.redact({
    url: "https://player.example/api?token=abc123&q=jazz&session=zzz&page=2",
    header: "raveplayer_session=deadbeef; theme=dark",
    bearer: "Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.sig",
    jwt: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
  });
  assert.match(out.url, /token=\[redacted\]/);
  assert.match(out.url, /session=\[redacted\]/);
  assert.match(out.url, /q=jazz/);
  assert.match(out.url, /page=2/);
  assert.match(out.header, /raveplayer_session=\[redacted\]/);
  assert.strictEqual(out.bearer, "Authorization: Bearer [redacted]");
  assert.strictEqual(out.jwt, "[redacted-jwt]");
});

test("long strings are truncated and cycles are safe", () => {
  const long = RaveDebug.redact({ note: "x".repeat(900) });
  assert.ok(long.note.length <= 301, "string should be capped, got " + long.note.length);
  const loop = { note: "fine" };
  loop.self = loop;
  const out = RaveDebug.redact(loop);
  assert.strictEqual(out.note, "fine");
  assert.strictEqual(out.self, "[circular]");
});

test("nested values keep their shape", () => {
  const out = RaveDebug.redact({
    job: { id: "abc", state: "downloading", bytes: 2048, extra: { token: "nope", keep: 1 } },
    list: [{ password: "a" }, "plain"],
  });
  assert.strictEqual(out.job.id, "abc");
  assert.strictEqual(out.job.bytes, 2048);
  assert.strictEqual(out.job.extra.token, "[redacted]");
  assert.strictEqual(out.job.extra.keep, 1);
  assert.strictEqual(out.list[0].password, "[redacted]");
  assert.strictEqual(out.list[1], "plain");
});

/* ------------------------------------------------------------ ring buffer */

test("record is a no-op while disabled", () => {
  RaveDebug.disable();
  RaveDebug.clear();
  assert.strictEqual(RaveDebug.record("api", { path: "/api/menu" }), null);
  assert.strictEqual(RaveDebug.size, 0);
});

test("the buffer keeps at most 500 events, oldest dropped", () => {
  RaveDebug.enable();
  RaveDebug.clear();
  for (let i = 0; i < 600; i += 1) RaveDebug.record("test", { i });
  assert.strictEqual(RaveDebug.size, RaveDebug.MAX_EVENTS);
  assert.strictEqual(RaveDebug.MAX_EVENTS, 500);
  const events = RaveDebug.snapshot().events;
  assert.strictEqual(events.length, 500);
  assert.strictEqual(events[0].data.i, 100);
  assert.strictEqual(events[499].data.i, 599);
  for (let i = 1; i < events.length; i += 1) {
    assert.ok(events[i].seq > events[i - 1].seq, "seq must increase");
    assert.ok(events[i].at >= events[i - 1].at, "timestamps must not go backwards");
  }
});

test("formatText emits one JSON object per event", () => {
  RaveDebug.clear();
  RaveDebug.record("route", { path: "/api/latest" });
  RaveDebug.record("media", { event: "play", path: "/api/stream" });
  const lines = RaveDebug.formatText().split("\n");
  assert.strictEqual(lines.length, 2);
  const first = JSON.parse(lines[0]);
  assert.strictEqual(first.kind, "route");
  assert.strictEqual(first.data.path, "/api/latest");
  assert.strictEqual(JSON.parse(lines[1]).kind, "media");
});

test("clear empties the buffer", () => {
  RaveDebug.record("api", { path: "/api/menu" });
  assert.ok(RaveDebug.size > 0);
  assert.strictEqual(RaveDebug.clear(), 0);
  assert.strictEqual(RaveDebug.size, 0);
  const snap = RaveDebug.snapshot();
  assert.strictEqual(snap.maxEvents, 500);
  assert.deepStrictEqual(snap.events, []);
});

test("event kinds are bounded", () => {
  RaveDebug.clear();
  const event = RaveDebug.record("k".repeat(80), { ok: true });
  assert.strictEqual(event.kind.length, 40);
});

console.log("PASS " + passed + " debug.js unit checks");
