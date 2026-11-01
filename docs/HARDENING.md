# Hardening pass — Rave Player web (`raveplayer-web` 1.6.12)

**Branch:** `hardening/1.6.12` (not pushed) · **Base:** `v1.6.11-review` (`e49b11d`)
**Scope:** `app.py`, `core/`, `static/app.js`, `scripts/deploy.sh`, `docs/CODE-REVIEW.md`,
`VERSION`. No UX or content changes. No `data/` or advert files touched.

**Result:** `205 → 230 passed` in `2.6 s` (`.venv/bin/python -m pytest -q test`), all
offline, no network. `node --check static/app.js` clean.

Inputs folded in: the internal task-22 brief and the adversarial pre-review
(`docs/ADVERSARIAL-REVIEW.md`, web findings W1–W8). The review's "what is solid" list
(stream SSRF guard, SPA escaping, CSP, content-type gate, atomic writes) was kept intact.

---

## Summary

| # | Finding | Severity | Status | Tests |
|---|---|---|---|---|
| H1 | `core/fetch.py` follows redirects into private/LAN addresses (SSRF) | P1 | fixed | `test_hardening.py` redirect tests |
| W2 | `OverflowError` on `inf`/`1e400` duration metadata (`/api/item`, feeds) | P2 | fixed | `test_archive.py`, `test_hardening.py` |
| W3 | Favourites lost-update race (`load → filter → save` unlocked) | P2 | fixed | `test_hardening.py::test_concurrent_favourite_adds…` |
| W4 | Deploy script has no rollback; old container removed before health | P2 | fixed | `test_deploy.py` (4 tests) |
| W5 | Cache prune only at worker import, fixed 400-file cap, first-5000 slice | P3 | fixed | `test_hardening.py` prune tests |
| W7 | `external.href` from provider data with no scheme check | P3 | fixed | `test_hardening.py::test_spa_filters…` |
| W8 | Truncated streams forward a stale `Content-Length` | P3 | fixed | `test_stream.py::test_truncated_stream…` |
| W1 | `/api/stream` open proxy + wildcard CORS | P2 | **partly fixed** — CORS removed, optional host allow-list added; default host allow-list deferred (see below) | `test_stream.py` (2 tests) |
| W6 | Docs/runtime disagree (interpreter, `core/http.py`) | P3 | fixed (docs) | `test_hardening.py::test_docs_match…` |
| H2 | Provider fetches read unbounded bodies (memory) | P2 | fixed | `test_hardening.py::test_fetch_response_size_cap` |
| H3 | Provider fetch failures logged full URLs (API keys in query) | P2 | fixed | `test_hardening.py::test_fetch_logs_redact…` |
| H4 | Radio mirror failover = 3 × 20 s on one request | P2 | fixed | `test_hardening.py::test_radio_mirror_failover…` |
| H5 | Cold `/api/podcasts` / `/api/search` add provider timeouts serially | P2 | fixed | `test_hardening.py` parallel tests |
| H6 | Feed entity expansion (billion laughs) mirror of add-on K5 | P2 | fixed | `test_hardening.py::test_feed_with_entity…` |

---

## Detail

### H1 — P1 — Redirects in `core/fetch.py` bypass every SSRF rule (new)

**Finding.** The stream proxy validates redirects hop by hop, but every provider
resolution endpoint (`/api/item`, `/api/dnb/<id>`, `/api/podcast/<id>`, `/api/legacy/*`,
feeds, Mixcloud/SoundCloud) fetches through `core/fetch.py`, which used plain
`urllib.request.urlopen` and therefore followed redirects anywhere. A third-party
feed/API/legacy host could redirect the server into the LAN
(`http://169.254.169.254/…`, `http://192.168.1.179:…`, the Docker host).

**Reproduced before the fix** (local loopback servers, no internet):

```
redirect port 53645  -> 302 Location: http://127.0.0.1:53644/secret
core.fetch.fetch("http://127.0.0.1:53645/redir") == 'SECRET-INTERNAL-RESPONSE'
```

**Fix.** `core/fetch.py` now uses an opener with `_SafeRedirectHandler`:
- every hop must be `http(s)`, credential-free, and resolve to public addresses
  (same private/loopback/link-local/multicast/reserved rules + extra networks as the
  stream proxy); all A/AAAA answers are checked;
- `RAVEWEB_ALLOW_PRIVATE=1` (the existing stream opt-in) also allows private redirects;
- redirect chain capped at `MAX_REDIRECTS = 3`; a refused redirect is logged once and
  the fetch fails closed (`None`).
The **initial** URL is deliberately not blocked: it comes from the app's own provider
code/registry, and a self-hosted user may point `feeds.json` at a LAN feed.

**Tests.** `test/test_hardening.py`: `test_fetch_redirect_to_private_address_is_refused`,
`test_fetch_redirect_to_bad_scheme_is_refused`, `test_fetch_redirect_chain_is_capped`,
`test_fetch_allows_private_redirects_when_opted_in`, plus a wiring test that the module
opener uses the safe handler. The old behaviour (loopback redirect) is the negative case.

### W2 — P2 — Non-finite duration metadata (`OverflowError`)

`int(float("inf"))` / `int(float("1e400"))` raise `OverflowError`, and the code only
caught `ValueError`, so one bad Archive item turned `/api/item/<id>` into a 500 and one
bad feed broke `/api/podcasts`.
Fixed in `core/archive.py::_duration`, `core/feeds.py::_duration`,
`app.py::_human_duration` and `core/radio.py::_as_int` (`math.isfinite` guards +
`OverflowError` catch; `bool` no longer counts as a duration).
Tests: `test_archive.py` (inf/1e400/nan/±float), `test_hardening.py`
(`test_feed_duration_ignores_non_finite_metadata`,
`test_item_with_non_finite_length_is_not_a_500`, `test_human_duration_…`).

### W3 — P2 — Favourites lost-update race

`add()`/`remove()` did an unlocked read-modify-write; two concurrent saves could each
read the same list and the second `os.replace` discarded the first. Writes were already
atomic, the *update* was not.
**Fix.** `core/favourites.py`: process-wide `threading.RLock` **plus** advisory
`fcntl.flock` on `<data>/.favourites.lock` around load→modify→save (the file lock covers
the two gunicorn workers; the lock is best-effort so a read-only data dir still behaves
like before).
**Tests.** `test_hardening.py::test_concurrent_favourite_adds_do_not_lose_entries` (8
threads behind a barrier, deliberately slow `save`; without the lock this yields 1 entry,
with it 8) and `test_concurrent_add_and_remove_keep_the_store_consistent`.

### W4 — P2 — Deploy script had no rollback

Old flow: copy data to `/tmp` → `rm -rf $DEST` → extract → build → `docker rm -f` the
running container → run → health check → exit 1. A build/run/health failure left the
service down and the only data copy in `/tmp`.
**Fix.** `scripts/deploy.sh` now:
1. keeps a host-side backup at `$DEST.data-backup` (never only `/tmp`);
2. builds and tags the new image **before** touching the old container (build failure ⇒
   the old container keeps serving);
3. stops (does not remove) `raveplayer-web`, starts the new one as `raveplayer-web-new`;
4. health-checks it; on failure removes the new container, restarts the old one and exits
   non-zero; on success removes the old container and renames the new one into place;
5. passes `SOUNDCLOUD_CLIENT_ID` in a `umask 077` env file (`--env-file`) instead of the
   `docker run` command line, and deletes the file immediately after start.
**Tests.** `test/test_deploy.py` (4): `sh -n` parse; version/image/label pins; rollback
markers + "stop before remove" + "data not only in /tmp" ordering; no `-e
SOUNDCLOUD_CLIENT_ID` / `--env-file` present.

### W5 — P3 — Cache pruning only at import

`_prune_cache()` ran once at worker import, removed at most 400 files and only examined
`os.listdir()[:5000]`; a long-lived worker never pruned again.
**Fix.** `app.py`: pruning now runs from a `before_request` hook, at most once per
`PRUNE_INTERVAL = 3600 s`, with counted work (`PRUNE_MAX_SCAN = 20000`,
`PRUNE_MAX_REMOVALS = 400`) instead of a list slice. It is wrapped so maintenance can
never break a request.
**Tests.** `test_hardening.py::test_cache_prune_runs_from_a_request_at_most_hourly` and
`test_cache_prune_work_is_capped_per_run`.

### W7 — P3 — `external.href` from provider data

`openEmbed()` assigned `external.href = item.external` with no scheme check; only CSP
`script-src 'self'` stopped a `javascript:` value from executing.
**Fix.** `static/app.js` adds `safeExternalHref(value, fallback)`: the value is parsed
with `new URL`, only `http:`/`https:` is accepted, otherwise the trusted provider home
page is used. `innerHTML` remains absent from the file (the existing test still enforces
it).
**Test.** `test_hardening.py::test_spa_filters_external_links_to_http_schemes`
(+ `node --check`).

### W8 — P3 — Stale `Content-Length` on truncated streams

`_passthrough_headers()` forwarded the upstream `Content-Length`, but `generate()` may
stop early (byte cap, deadline, upstream stall), so clients could wait for bytes that
never arrive. The same header was also wrong on the synthetic 416 response.
**Fix.** `Content-Length` is not forwarded; the body is chunked (`Content-Range`,
`Accept-Ranges`, `ETag` etc. are still passed through for seeking).
**Test.** `test_stream.py::test_truncated_stream_does_not_carry_a_stale_content_length`
and the existing 206/416 tests (which assert the remaining headers).

### W1 — P2 — `/api/stream` open proxy + wildcard CORS (partly fixed)

The proxy validly fetches any **public** URL (that is how hotlink-protected streams,
arbitrary live-radio hosts and podcast CDNs work) and is LAN-only with no auth by design.
The concrete cross-site risk was the response header `Access-Control-Allow-Origin: *`,
which let any web page a LAN user visited read the proxy's bytes.
**Fixed.** Removed the wildcard ACAO (audio is same-origin through this route, so nothing
needs it) and added `Cross-Origin-Resource-Policy: same-origin`. Added an **opt-in**
destination allow-list: `RAVEWEB_STREAM_HOSTS=archive.org,rinse.fm,…` (suffix match,
applied to the initial URL and every redirect hop, before DNS). Default stays empty so
live radio keeps working.
**Deferred (product decision):** making the host allow-list the *default* would break the
live-radio directory, which lists stations on thousands of arbitrary public hosts — that
needs an owner decision (allow-list per section, or accept LAN-only open proxy). The
safe default implemented here is "same-origin only, no cross-site reads, opt-in
destination pinning", documented in the script/README env list below.
**Tests.** `test_stream.py::test_stream_route_does_not_send_a_wildcard_cors_header` and
`test_stream_host_allowlist_is_optional_and_scoped`; the existing SSRF suite is unchanged
and still green.

### W6 — P3 — Docs vs runtime

`docs/CODE-REVIEW.md` said Python 3.11 / `python:3.11-slim` (Dockerfile and README are
3.12) and named `core/http.py` (the module is `core/fetch.py`).
**Fix.** Docs now state Python 3.12 / `python:3.12-slim`, note that the local `.venv` used
by the test command is older and CI runs 3.12, and name `core/fetch.py`.
**Test.** `test_hardening.py::test_docs_match_the_container_interpreter_and_module_layout`
asserts `Dockerfile`, `README.md` and `docs/CODE-REVIEW.md` agree on the interpreter and
that the module layout matches.

---

## Other hardening in this pass (not in the review list, same classes)

| # | Finding | Severity | Fix | Test |
|---|---|---|---|---|
| H2 | Provider fetches read the whole body into memory (a hostile/broken upstream can stream GBs); gzip/deflate not capped either | P2 | `core/fetch.py`: raw read, gzip and deflate paths capped at `MAX_BODY_BYTES = 8 MiB`; oversize → `None` | `test_hardening.py::test_fetch_response_size_caps` (raw + gzip + truncated-deflate) |
| H3 | Provider fetch failures logged the **full URL**, including `SOUNDCLOUD_CLIENT_ID` and signed query strings | P2 | `core/fetch.py::_safe_url` logs scheme/host/path only (no query/fragment/userinfo) for fetch failures and bad-JSON logs | `test_hardening.py::test_fetch_failure_logs_redact_query_secrets` |
| H4 | `core/radio.py`: three dead mirrors meant 3 × 20 s in one request | P2 | per-hop `MIRROR_TIMEOUT = 8 s` + overall `MIRROR_DEADLINE = 15 s`, shared by tag and name lookups | `test_hardening.py::test_radio_mirror_failover_is_bounded` |
| H5 | Cold `/api/podcasts` fetched 5 feeds serially (5 × 25 s); cold `/api/search` fetched widen + SoundCloud + Mixcloud serially | P2 | `core/feeds.counts()` runs counts in a bounded pool (`COUNT_WORKERS = 4`, `COUNT_TIMEOUT = 12 s`); `app._gather()` runs the independent search jobs together (`SEARCH_WORKERS = 3`), preserving output order | `test_hardening.py` parallel/count tests + existing `test_speedups` count cache tests |
| H6 | Feed parsing used `xml.etree.ElementTree` on third-party RSS: internal entity expansion (billion laughs), mirroring add-on K5 | P2 | `core/feeds.py::parse` refuses documents containing `<!ENTITY` before parsing | `test_hardening.py::test_feed_with_entity_declarations_is_refused` |

---

## Verified, deliberately not changed (do not regress)

- `/api/stream` SSRF logic: scheme/host/port checks, credentials and control chars
  rejected, **all** DNS answers validated, chosen IP pinned for the connection
  (DNS-rebinding defence), manual redirects with per-hop validation, private/loopback/
  link-local/multicast/reserved + extra networks denied, `RAVEWEB_ALLOW_PRIVATE` defaults
  to deny, 6-stream semaphore, wall-clock deadline, byte cap. `test/test_stream.py` still
  covers all of it.
- SPA escaping: no `innerHTML`/`insertAdjacentHTML`/`document.write`; third-party strings
  go through `textContent`; `templates/index.html` has one external script and no inline
  handlers, so the CSP does not need loosening.
- Stream content-type gate + forced `audio/mpeg` + `nosniff` (proxied-URL-to-XSS).
- Atomic favourites/cache writes (temp + fsync + `os.replace`).

## Known limits / not done

- The deploy rollback path is covered by string/ordering tests plus `sh -n`, not exercised
  against a live Docker host (this environment has no Docker daemon for that host).
- `W1` default host allow-list deferred (product decision, see above).
- The browser suite (`test_e2e`, 7 cases) was not run: the `.venv` here has no Playwright
  package (`requirements-browser.txt` is test-only and not installed in this checkout).
- No dependency/version change, so no wheel check was needed for 3.12; docs now match the
  Dockerfile.

## How to reproduce

```sh
cd "Rave Player"
.venv/bin/python -m pytest -q test        # 230 passed
node --check static/app.js
sh -n scripts/deploy.sh
git diff --stat main..hardening/1.6.12
```

## Post-merge fix — deploy.sh heredoc escaping (found on first real deploy)

The hardened rollback script failed on its first live run with
`DATA_BACKUP: unbound variable`: remote-only variables were written as
`\$DATA_BACKUP` inside the unquoted heredoc but their **assignment** was not
escaped/expanded consistently, so `set -u` aborted locally before `ssh` ran.
Fixed by expanding `$DEST` locally in the assignment (`DATA_BACKUP="$DEST.data-backup"`)
and escaping every remote read (`"\$DATA_BACKUP"`); `SOUNDCLOUD_CLIENT_ID` is
expanded locally so the secret travels only over ssh stdin into the root-only
env file. `test_deploy.py` now asserts the correct form and that the unescaped
form is absent. The real deploy then completed: 1.6.12 healthy, rollback path
(new container aside → health check → rename) exercised end to end.
