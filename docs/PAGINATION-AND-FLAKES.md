# Pagination and test determinism — 1.6.53 (integration pending)

Branch `feat/pagination-1.6.53` (worktree
`rave-player-worktrees/pagination-1.6.53`), based on
`integration-1.6.52` @ `aa9d713`.  The work commit is the tip of this branch.  No version bump: the integration release was
pending when this branch was cut.

## What changed

- Every list pages at **40 rows/page** and returns the same pager object:

  ```json
  {"page": 2, "pages": 66, "total": 2640, "per_page": 40,
   "prev": "...&page=1", "next": "...&page=3",
   "links": [1, 2, 3, 4, 66]}
  ```

  `next` is still emitted for older callers.  `links` is a bounded window
  (`None` marks an ellipsis), never one link per page.
- Covered: Toronto genre/search (679/2,640), Moxser (1,001), JungleTapes
  (310, now 40/page instead of 50), Rave Nights (55), Archive sections/search/
  year/filter/pack detail (1,645), every-pack, search, downloads, live radio
  (up to 500), podcast episodes, Hardscore, GetDarker, Rave Archive, Latest,
  Mixcloud results and the RTP category index / pack details.
  RTP WordPress pages only expose “is there a next page”, so those use a
  cursor pager (`pages: null`, Previous/Next, “Page N”).
- Listing work is O(page): the visible window is sliced before item dicts are
  built. Toronto page 2 of 2,640 dropped from ~37 ms to ~12 ms warm on this
  host; the page-1 response is 12.6 KB instead of the whole collection.
- UI: `#page-meta` shows `Page N of M · X items`; top and bottom
  `<nav aria-label="Pagination">` strips render `Previous 1 2 3 … N Next` with
  `aria-current="page"`, disabled prev/next at the bounds, 44 px touch targets
  and wrap on a phone. Page changes call `navigate()` so the hash keeps
  `?page=` and browser Back/Forward restores that page and its rows. Focus
  moves to the list heading after a pager click; ArrowLeft/ArrowRight work
  while focus is inside the pager.

## Test matrix (this host, macOS, Python 3.9.6)

Web offline (`pytest test`) and browser (`pytest test_e2e`, serial, Playwright
Chromium at `PLAYWRIGHT_BROWSERS_PATH=.pw-browsers`):

| Run | Before this branch | After |
|---|---|---|
| Offline ×3 | 531/531 ×3 (green) | 544/544 ×3 (green) |
| Offline shuffled test order ×2 | not measured | 544/544 ×2 (green) |
| Browser e2e ×2 serial | **35/36 then 34/36** | 38/38 ×2 (green) |

Kodi (`raveplayer-worktrees/pagination-3.9.50`) is reported in
`docs/PAGINATION-AND-FLAKES.md` there: 715/715 ×9 (plain, temp-cache and
shuffled order) after the fixes.

## Flakes found and fixed

1. **`test_ravetapepacks_source_blocked_stays_silent_no_loop` was red 2/2.**
   It was written for the 1.6.50 "nothing at all" behaviour, but 1.6.52 P0-3
   added the quiet idle line `Source busy — retrying quietly…`. The test now
   asserts that line plus no banner/toast/retry button and no automatic loop —
   the product behaviour, not the stale expectation.
2. **`test_loading_pill_waits_150ms_and_clears` flaked (1/2).** The "fast"
   `/api/packs` request could cross the 150 ms grace on a loaded host, so the
   pill legitimately appeared. The test now fulfils `/api/packs` locally with a
   fixed JSON body, which makes "fast" a property of the test rather than the
   machine.
3. **`test_shared_store_across_processes_no_clobber` flaked in the full
   suite.** The app's in-process download scheduler thread, left running by an
   earlier test, claimed the two jobs this test creates by hand, so
   `claim_next()` found no queued row. The scheduler now has a pause gate;
   `test/conftest.py` clears it after every test and the cross-process test
   pauses it explicitly before creating rows. This is also stricter isolation:
   no background worker crosses a test boundary.
4. **`test_ravetapepacks_source_blocked_auto_retries_silently` flaked (~2/5).**
   The fake 200 body was `ID3` + NUL bytes with an `audio/mpeg` type; Chromium
   sometimes raised `MEDIA_ERR_SRC_NOT_SUPPORTED` after the test's `src` wait
   and showed "This stream can't be played here". The stub now returns a valid
   tiny silent WAV, so a media-decode error cannot masquerade as an RTP-source
   failure. 5/5 green after the change.

Shared-directory hygiene: the offline and e2e conftests already created a
fresh `RAVEWEB_DATA`/cache/favourites tree; the scheduler gate removes the
remaining process-lifetime state. `bench_api.py` and the new
`bench_pagination.py` use throwaway roots and fake `core.fetch._request`, so
they never touch the live Internet.

## Timings (hermetic host render, not a device)

`scripts/bench_pagination.py` replaces every provider with a deterministic list
and drives the real Flask test client. Cold = first request for that route in
the process; warm = next five. These are host render/serialise numbers, not
phone or device numbers; run-to-run variance on this shared host was large
(roughly 2× under load), so compare the shape, not the last millisecond.

| List | Page | Items | Cold ms | Warm p50 ms | Warm p95 ms | Bytes |
|---|---|---|---|---|---|---|
| Toronto 2,640 | 1 | 40 | 51.1 | 12.2 | 14.5 | 12,640 |
| Toronto 2,640 | 66 | 40 | 21.4 | 12.6 | 12.7 | 12,782 |
| Moxser 1,001 | 1 | 40 | 7.4 | 7.6 | 7.7 | 6,837 |
| Moxser 1,001 | 26 | 1 | 7.2 | 7.2 | 8.8 | 339 |
| JungleTapes 310 | 1 | 40 | 6.9 | 6.0 | 6.5 | 7,940 |
| JungleTapes 310 | 8 | 30 | 6.8 | 5.9 | 6.4 | 5,970 |
| Rave Nights 55 | 1 | 40 | 5.3 | 5.4 | 6.1 | 4,249 |
| Rave Nights 55 | 2 | 15 | 5.2 | 5.8 | 6.1 | 1,682 |
| Pack 1,645 | 1 | 40 | 7.1 | 7.4 | 7.7 | 9,992 |
| Pack 1,645 | 42 | 5 | 5.8 | 7.0 | 7.5 | 1,479 |
| Search 2,400 | 1 | 52 | 231.7 | 6.4 | 7.4 | 11,646 |
| Search 2,400 | 60 | 40 | 10.9 | 4.7 | 5.0 | 6,620 |

The e2e pager test also verifies the same lists in Chromium at
1280 × 900 and 390 × 844 (no horizontal overflow, 44 px targets, Back/Forward).
There is no physical phone/tablet in this environment; the 390 × 844 Chromium
viewport is the phone check.

## Commands

```sh
cd rave-player-worktrees/pagination-1.6.53
.venv/bin/python -m pytest test -q                     # 544 offline
.venv/bin/python -m pytest test_e2e -q                 # 38 browser
PLAYWRIGHT_BROWSERS_PATH=... RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1 \
  .venv/bin/python -m pytest test_e2e -q -p no:randomly

# Shuffled order used to prove there is no order dependence:
SHUFFLE_SEED=11 PYTHONPATH=/tmp .venv/bin/python -m pytest test -q -p shuffle_plugin

# Hermetic timings:
.venv/bin/python scripts/bench_pagination.py --samples 5 --json /tmp/web-page.json
```

(The shuffled runner is a 6-line `pytest_collection_modifyitems` plugin that
does `random.Random(seed).shuffle(items)`.)

## Repeatability

After the fixes: 544/544 offline ×3 + shuffled ×2, 38/38 e2e ×2. No deploy,
push or version bump was performed.
