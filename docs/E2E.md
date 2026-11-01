# Browser e2e run — `test_e2e` (Playwright), 1.6.19 tree

Owner: motion-1x1 · Task: close the last deferred web item ("actually run the
browser suite"). Branch `hardening/1.6.13` (this e2e change was test+docs only;
"Branch / version" below).

## Result

| Run | Command | Result |
|---|---|---|
| Offline suite (baseline) | `.venv/bin/python -m pytest -q test` | **311 passed** |
| Browser suite, no local browser | `.venv/bin/python -m pytest test_e2e -q` | 7 skipped (clean skip, as designed) |
| Browser suite, system Chrome | see command below | **18 passed** |

```
$ .venv/bin/python -m pytest -q test
311 passed

$ RAVEWEB_REQUIRE_BROWSER=1 RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1 \
  RAVEWEB_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  .venv/bin/python -m pytest test_e2e -q
7 passed
```

`RAVEWEB_REQUIRE_BROWSER=1` turns the "no browser" skip into a failure, so a
broken install cannot pass silently (the CI browser job already sets it). The
test module covers 18 cases: boot + the grouped 9-button rail, hub and section
navigation, the rail fitting 1440×900 with Favourites visible, the pinned 48 px
back button under scroll, Surprise Me opening a random pack/show, the phone
rail/back layout, the Download action → Downloads library → offline playback → delete
round trip, Mixcloud paste → embed panel (src/provider/href/title, close),
SoundCloud paste → panel + backdrop close, plain text search, CSP `frame-src`
allowing the widget hosts and refusing anything else, and a favourites
add/remove round trip.
The suite is offline: the Flask app runs on a
random localhost port with a temporary `RAVEWEB_DATA`, providers are stubbed and
external requests are intercepted.

## Setup in this environment

`requirements-browser.txt` (Playwright 1.60.0 + `requirements-dev.txt`) is
**already satisfied** in `.venv` (verified with `importlib.metadata`: playwright
1.60.0, Flask 3.0.3, gunicorn 22.0.0, pytest 8.4.2). `pytest test_e2e` skips
cleanly when the browser binary is missing; CI installs it with
`python -m playwright install chromium`.

In this sandbox Playwright's own Chromium **cannot be installed**, because the
browser cache lives outside the writable workspace:

```
$ .venv/bin/python -m playwright install chromium
Failed to install browsers
Error: EPERM: operation not permitted, mkdir '/Users/theyid/Library/Caches/ms-playwright'
```

Pointing `PLAYWRIGHT_BROWSERS_PATH` at the writable workspace got past the mkdir
but Playwright's installer could not take its directory lock there:

```
$ PLAYWRIGHT_BROWSERS_PATH=…/.pw-browsers .venv/bin/python -m playwright install chromium
Error: Unable to update lock within the stale threshold Path: …/.pw-browsers/__dirlock
    at …/playwright/driver/package/lib/coreBundle.js:29236:21
```

**Workaround used (documented by the suite):** reuse the already-installed
system Chrome through `RAVEWEB_CHROMIUM_EXECUTABLE`, with
`RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1` for this sandbox. System Chrome is
`Google Chrome 156.0.8078.74`; Playwright 1.60 drives it successfully. This is
exactly the hook the README lists for local use — no test or app change needed
to run the suite. (CI keeps installing Playwright's own Chromium.)

The app is **not** started by hand: `test_e2e/conftest.py` serves the real
Flask app with `werkzeug.make_server` on a random localhost port, isolated data
dirs, and provider stubs. There is no separate `app.py` process to manage.

## Failures found and fixed (test expectations only — no UX/app change)

Both failures were stale hardcoded counts in the e2e tests, from product
additions made after the suite was written; the app rendered the correct UI in
each case.

1. **`test_boot_renders_the_shell_and_all_fifteen_sections` — 16 nav buttons
   vs expected 15.** The product has had **16** sections since the Rinse FM
   section landed in 1.6.10 (`SECTIONS` in `app.py`, `/api/menu` returns 16).
   The test name, docstring and assertion were all pre-Rinse.
   **Fix:** the test now derives the expected count from `fetch('/api/menu')`
   and asserts the nav renders one button per advertised section, plus that
   named sections (`Latest`, `Rinse FM`, `Favourites`, `Search`) are present.
   Renamed to `test_boot_renders_the_shell_and_all_sections`.

2. **`test_section_navigation_renders_rows` — 16 Quick Filter rows vs expected
   15.** `filters.json` currently has 16 entries (`kool`, `rinse`,
   `dreamscape`, … `world-dance`); `/api/filters` returns 16 and the page
   rendered 16. **Fix:** the test derives the expected rows from
   `fetch('/api/filters').items` and additionally compares every rendered row
   label with the API labels (stronger than the old count-only assertion).

Result: 2 failed / 5 passed → **7 passed**. No `app.py`, `static/app.js`,
template or UX change was needed; the 1.6.15 tree keeps 285 offline tests green, and
the rail/back/Surprise/theme work has its own browser cases (17 total, incl. the
spinner progress panel, loading pill and reduced motion).

## Branch / version

Branch **`hardening/1.6.13`** (base `main` = 1.6.12 + the 1.6.12 hardening
work). Only `test_e2e/test_smoke.py`, `docs/E2E.md` and the README browser
section changed — **no runtime code**, so `VERSION` is **not** bumped
(the e2e fix itself was test-only, so it did not bump VERSION at the time; the
branch now carries the 1.6.13 runtime fixes from the independent review, which is why
VERSION is 1.6.13 on this branch. 1.6.12 remains the live/deploy version until 1.6.13
is released.)

## Artifacts

None. These tests assert DOM/API state and do not capture screenshots or
traces; the server's temporary data directories are created under the system
temp dir and removed by the fixtures. Failure diagnostics come from the
Playwright assertion messages (as shown above).

## Screenshots (1.6.15)

Set `RAVEWEB_ARTIFACTS_DIR` when running the browser suite to write the review
screenshots (not committed; kept with the working notes):

```
RAVEWEB_ARTIFACTS_DIR="$PWD/rave-player-space/reports/web-1.6.17" \
  .venv/bin/python -m pytest test_e2e -q
```

`01-latest-desktop-1280`, `02-rail-1440x900`, `03-back-pinned-scrolled`,
`04-surprise-result`, `05-phone-back-and-rail`, `06-downloads-view`,
`07-download-panel`, `08-loading-pill`, `09-surprise-reveal`.

## Reproduce

```sh
cd "Rave Player"
# offline suite (what ships with the image):
.venv/bin/python -m pytest -q test                       # 311 passed

# browser suite here (system Chrome; CI uses `playwright install chromium`):
RAVEWEB_REQUIRE_BROWSER=1 RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1 \
RAVEWEB_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  .venv/bin/python -m pytest test_e2e -q                 # 7 passed
```
