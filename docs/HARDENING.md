# Hardening pass — Rave Player web (`raveplayer-web` 1.6.17)

**Branch:** `feature/1.6.17` (not pushed) · **Base:** `main` @ `645f22a` (1.6.16)
**Scope:** `app.py`, `core/`, `static/app.js`, `scripts/deploy.sh`, `docs/`, `test/`,
`test_e2e/`, `VERSION`. No UX or content changes. No `data/` or advert files touched.

**Result:** `205 → 302 passed` (`.venv/bin/python -m pytest -q test`), all
offline; browser suite `test_e2e` **17 passed** (system Chrome, Playwright 1.60);
`node --check static/app.js` and `sh -n scripts/deploy.sh` clean.

Inputs folded in: the internal task-22 brief and the adversarial pre-review
(`docs/ADVERSARIAL-REVIEW.md`, web findings W1–W8). The review's "what is solid" list
(stream SSRF guard, SPA escaping, CSP, content-type gate, atomic writes) was kept intact.


## 1.6.17 — spinner pass (progress panel, loading pill, Surprise reveal)

UI/animation release; the security surface is unchanged:

- **No new fetch path:** the NDJSON progress stream is the same proxy-save code
  (`_download_chunks` via `_open_stream`), just yielding progress lines. The
  per-file/total/free-space caps, source allow-list and atomic write are
  unchanged. Cancel only adds the client id to an in-memory set checked once per
  chunk; the partial file is discarded in a `finally` (including client
  disconnect), and ids are regex-validated.
- **Progress endpoint** `POST /api/downloads` with `Accept: application/x-ndjson`
  streams `started`/`progress`/`saved`/`cancelled`/`error`; default clients still
  get the original JSON. `POST /api/downloads/cancel/<client_id>` validates
  `[A-Za-z0-9_-]{6,64}` and is best-effort.
- **Spinner art** is the Kodi 2026 pack copied verbatim (10 × 128/256 px); the
  sprite sheets are derived tiling, not redrawn. CSS is `steps(10)` with
  `image-set` for 2x; `prefers-reduced-motion` disables all spinner/bar/stamp
  animation. Loading pill is aria-live and only appears after 150 ms; no
  innerHTML; CSP unchanged (only same-origin sprite PNGs).
- Tests: 302 offline (+9 `test/test_spinner.py`) and 17 e2e (download panel +
  cancel, pill grace/clear, Surprise spin/stamp, reduced motion).
  Screenshots: `rave-player-space/reports/web-1.6.17/07-09*.png`.

## 1.6.16 — Downloads/offline, phase 1 (web side)

New feature; the only new network path is a server-side proxy save that
deliberately reuses the stream proxy's guards:

- **Eligibility is a server-side allow-list** (`DOWNLOAD_SOURCES`,
  `_download_allowed`): only direct `.mp3`/`.m4a` http(s) URLs from Archive
  tracks, Moxser resolved MP3s, Hardscore MP3/M4A and curated Pirate direct
  files. Platform hosts (Mixcloud/SoundCloud/YouTube/Vimeo), non-media pages,
  credentials, non-http(s) and non-file paths are refused; each source also has
  its own host check. The client cannot label a provider page as an MP3.
- **Proxy save reuses `_open_stream`**, so the SSRF blocklist, all-DNS-answer
  validation, DNS pinning, hand-rolled redirect re-validation and
  connect/read deadlines apply unchanged. Byte counts are capped per file
  (`RAVEWEB_DOWNLOAD_BYTES`, default 500 MiB), in total
  (`RAVEWEB_DOWNLOAD_TOTAL`, default 2 GiB), by item count (200) and by free
  space, before and while writing; `Content-Length` is checked up-front; the
  file is written atomically (temp + rename) under `RAVEWEB_DATA/downloads`.
- **Safe output:** filenames are sanitised title stems + URL hash + `.mp3`/`.m4a`;
  `/api/downloads/<id>/file` serves the local copy with `Content-Disposition:
  attachment` and Range support; `/stream` is the same-origin offline player
  (Range, CORP same-origin, nosniff). Delete removes file + index atomically.
- **No auto-download** and no client-side storage: the user must press the
  Download action; the Downloads view is read from the server library.
- **Out of scope phase 1** (also shared with the Kodi add-on): podcast/RSS
  enclosures, Legacy-scraper tracks, GetDarker rows, live radio, HLS.
- Tests: 293 offline (+8 `test/test_downloads.py`) and 13 e2e (download →
  Downloads view → offline play from `/api/downloads/<id>/stream` → delete, and
  no Download action on podcast-like rows). Screenshot:
  `rave-player-space/reports/web-1.6.16/06-downloads-view.png`.

## 1.6.15 — rail, pinned back button, Surprise Me, brutalist theme pass

UI/chrome release. The security surface is unchanged:

- **8-button grouped rail** (`RAIL` + `SECTION_PATHS` in `app.py`,
  `/api/rail/<id>` hubs). All 21 sections stay reachable; the flat 21-button
  rail no longer overflows and hides the last buttons at 1440×900. The rail is
  still built with DOM `textContent` only — the catalogue code is an
  `aria-hidden` span so it never pollutes the accessible name.
- **Back button** is a 48 px sticker, `position: sticky` under the masthead
  (so it survives scrolling), with a 2 px paper border and magenta hard shadow;
  on phones it clears the sticky 58 px rail.
- **`/api/surprise`** chooses one random candidate from the existing Archive
  pack search + feed registry and returns only an internal `open` path; no new
  fetch path, no user-supplied input.
- **Theme pass** (`docs/THEME-WEB-2026.md`): CSS tokens now mirror the Kodi
  `docs/THEME-2026.md` pack (ink/paper/acid/magenta/cyan/orange, Impact display,
  halftone + scanline print texture, hard sticker shadows). The logo/mark,
  wordmark and mascot are untouched; no inline scripts, no `innerHTML`, and the
  CSP is unchanged (widget hosts stay as-is; SoundCloud remains on web).
- Tests: 285 offline (+11 `test/test_nav.py`) and 12 e2e (rail fits 1440×900,
  Favourites visible, pinned back under scroll, Surprise Me opens a pack/show,
  phone rail/back). Screenshots: `rave-player-space/reports/web-1.6.15/`
  (generated with `RAVEWEB_ARTIFACTS_DIR`).

## 1.6.14 — new mirror sources (Moxser, Hardscore, Rave Archive UK, GetDarker, pirate archive)

Feature release; the security surface is unchanged and deliberately reuses the
hardened fetch/proxy paths rather than adding a second networking stack.

- **Adapters:** `core/moxser.py`, `core/hardscore.py`,
  `core/ravearchive.py`, `core/getdarker.py` all fetch through
  `core/fetch.py` (public-address redirect guard, 8 MiB cap, wall-clock
  deadline, log redaction) with aggressive TTL caching and fail-soft parsing.
  Rave Archive/GetDarker/Hardscore never touch the network in tests (fixtures +
  monkeypatched fetch); `core/sources.py` holds the shared RSS/HTML helpers.
- **Moxser** walks the sitemap index → `section-mixes` sitemaps → mix pages and
  extracts title/date/flyer/direct MP3. It is throttled to ≤1 request/second
  and cached (sitemaps 1 h, pages 6 h). The MP3 host is public HTTPS
  (`enceladus.feralhosting.com`); `/api/stream` needs **no change** to serve
  it and its Range support was verified end-to-end (206 + `Content-Range:
  bytes 0-2047/45553738`, `audio/mpeg`, real ID3 bytes) — the SSRF guard still
  re-validates the host, pins DNS and strips wildcard CORS.
- **Curated pirate archive:** `core/pirate.py` reads
  `resources/pirate-radio.json` (5 hand-checked entries across Risk FM 107.1,
  Weekend Rush 92.3 and Flava FM), schema exactly `station, year, title, djs,
  source_url, stream_url, credit`. Entries whose URL could not be confirmed
  (Flava FM/A-Team) keep the metadata, leave `source_url` empty and carry the
  dead-MegaUpload note in `credit`; no link is invented.
- **UI:** five new sections in `/api/menu`; rows without direct audio get a
  safe http(s)-only `external` fallback (`safeExternalHref` + `window.open`)
  and mixed sources open their Mixcloud widget through the existing CSP
  `frame-src`. No `innerHTML`, no CSP change.
- **Tests:** `test/test_sources.py` (17 offline tests: parsers, routes, schema,
  stream target) plus a browser case for the Pirate Radio Archives tile.
  Playwright/offline fixtures replaced `core.fetch` so no test hits the
  Internet.

## 1.6.13 — independent-review follow-up (W1–W14)

Second-round findings from `docs/INDEPENDENT-REVIEW-2026-11.md` (web verdict: SHIP WITH
NOTES, 3 × P2). Every P2 and P3 is addressed; nothing is deferred except the documented
open-proxy product decision (W14).

| # | Sev | Finding | Fix | Test evidence |
|---|---|---|---|---|
| W1 | P2 | `/api/item` 500 on a colon-form huge `length` (`"1:" + "9"*400`) | `archive._duration`/`feeds._duration` reject >6-digit colon parts and durations >24 h; `_human_duration` catches `OverflowError` from `float(huge_int)` and caps at 24 h (covers Python 3.12's 4300-digit `int()` limit because no huge string reaches `int()`) | reproductions now return **200**; `test_hardening.py` W1 tests (units + `/api/item` for colon-form and `10**5000`) |
| W2 | P2 | `core/fetch._decode` had no wall-clock bound (12.03 s read with `timeout=2`) | body read via `read1` in 64 KiB chunks with a monotonic deadline; gzip streams and deflate inflate chunked with the same deadline; 8 MiB cap unchanged | `test_decode_respects_a_wall_clock_deadline` (fake slow-drip: `<1 s` vs unbounded), `test_request_applies_its_timeout_as_a_whole_fetch_budget`, existing cap tests |
| W3 | P2 | `deploy.sh`: `docker run` failing after `docker stop` aborted under `set -eu` → no rollback, env file with SoundCloud ID left in `/tmp` | single `restore_previous()` rollback path used for run failure and health failure; `trap 'rm -f "$ENV_FILE"' EXIT INT TERM`; build happens before the old container is touched | new functional tests run the extracted remote script against a **stubbed docker/curl**: run-fail restarts old + removes env file (0600, secret present only at run time), unhealthy rollback, healthy promote, build-fail leaves the old container running |
| W4 | P3 | `raves.registry` `int(“1,200”)` 500s `/api/raves` | `_count()` guard → 0 | `test_malformed_rave_catalog_counts_do_not_break_the_section` (registry + route) |
| W5 | P3 | cold `/api/latest` with 7 loaders × 4 workers > SPA 40 s abort | `MERGE_WORKERS = 7`, capped by loader count → one round, ~25 s worst case | barrier test: all 7 loaders run concurrently, no broken barrier |
| W6 | P3 | feed entity scan only looked at the first 64 KiB | scan the whole (8 MiB-capped) document | entity declaration after a 70 KB prefix is refused |
| W7 | P3 | feed `enclosure_url` accepted `javascript:`/`file:`/relative play URLs | `_media_url()` only returns http(s) (mirrors the add-on) | three bad schemes dropped, https kept |
| W8 | P3 | cache entry without `"value"` re-read forever; no read size cap | `_read` drops valueless entries and files over `MAX_ENTRY_BYTES` (8 MiB) | valueless + oversized entries removed and treated as misses; normal put/get still works |
| W9 | P3 | DNBRADIO title without `" : "` became both title and date | `DATE_RE` guard (add-on parity) | separator-less and dated titles |
| W10 | P3 | legacy scraper accepted `..`/empty path segments, duplicate URLs, double Referer suffix | `_url_path` rejects bad segments; `packs()` de-dupes URLs; `playable()` never appends twice | path/traversal/dedupe/playable tests |
| W11 | P3 | `/api/ukg` rendered raw Archive titles (list/huge) | reuse `_pack_items` (`_clean_text` + limit) | `test_ukg_rows_are_cleaned_and_bounded` |
| W12 | P3 | cache prune ran **on the request thread** (first request of the hour could stall) | prune now runs in a daemon thread, one at a time (`_PRUNE_LOCK`), triggered by a cheap hourly check | request latency test (<0.25 s with a slow prune), hourly guard, capped-work test |
| W13 | P3 | redirect hops each got the full provider timeout | `_request` sets an absolute deadline; `_SafeRedirectHandler` clamps each hop to the remaining budget and refuses past-deadline redirects | clamp + budget-exhausted tests |
| W14 | P3 | `/api/stream` open-proxy default undocumented | README proxy-security section now states the LAN-only unauthenticated open-proxy default, the `RAVEWEB_STREAM_HOSTS` pin, and the no-CORS/CORP behaviour | README assertion test |

**Stale docs corrected:** the 1.6.12 entries above that said the body read was only
size-capped (now deadline-bounded), that pruning ran from `before_request` on the
request thread (now off-thread), that the entity scan was a prefix window (now the
whole document), and that the deploy rollback covered only health failures (now also
`docker run`, with the env file removed by a trap). Test counts and the reproduce
command below are updated; `docs/CODE-REVIEW.md` and `docs/E2E.md` are refreshed to
1.6.13.

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
**Fix.** `app.py`: pruning runs at most once per `PRUNE_INTERVAL = 3600 s`, with
counted work (`PRUNE_MAX_SCAN = 20000`, `PRUNE_MAX_REMOVALS = 400`) instead of a list
slice. Since 1.6.13 (W12) it runs in a daemon thread triggered by a cheap request-hook
check, so `/data` maintenance can never stall a request; a lock keeps it to one run at
a time.
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
| H2 | Provider fetches read the whole body into memory (a hostile/broken upstream can stream GBs); gzip/deflate not capped either | P2 | `core/fetch.py`: read in 64 KiB `read1` chunks with an 8 MiB cap **and a monotonic deadline** (1.6.13/W2); gzip/deflate chunked with the same bound | `test_hardening.py::test_fetch_response_size_caps`, `test_decode_respects_a_wall_clock_deadline` |
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

- 1.6.12's deploy rollback was covered by string/ordering tests only. Since 1.6.13 the
  extracted remote script is executed with a **stubbed docker** (run-fail, unhealthy,
  healthy, build-fail) — still not a live Docker host, but the control flow is exercised.
- `W1` default host allow-list deferred (product decision, see above): live radio needs
  arbitrary public hosts; the default remains "no CORS, opt-in `RAVEWEB_STREAM_HOSTS`".
- Browser suite: **run and green** (7/7, system Chrome) — see `docs/E2E.md`. Playwright's
  own Chromium cannot be installed in this sandbox (EPERM on the home cache dir).
- No dependency/version change beyond `VERSION`; docs match the 3.12 container.

## How to reproduce

```sh
cd "Rave Player"
.venv/bin/python -m pytest -q test        # 302 passed
node --check static/app.js
sh -n scripts/deploy.sh
git diff --stat main..feature/1.6.17
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
