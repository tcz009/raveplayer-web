# -*- coding: utf-8 -*-
"""Headless-browser smoke tests: the class of bugs the offline suite missed.

Covers page boot + the grouped 7-button rail (derived from `/api/menu.rail`),
hub navigation and section rows, the pinned back button, Surprise Me (reveal
spin/stamp and the reduced-motion skip), the global 150 ms loading pill, the
spinner progress panel (save/cancel), the pasted Mixcloud/SoundCloud embed panel
(widget src, provider label, close and backdrop behaviour, CSP not refusing the
iframes), normal text search, and a favourites add/remove round trip.  Set
``RAVEWEB_ARTIFACTS_DIR`` to also write the review screenshots.

These are deterministic and offline: the Flask app is started locally by the
shared fixtures and every provider call is stubbed (see conftest.py).  Run
with ``pytest test_e2e``; they are not part of ``pytest test``.
"""

import json
import os
import time

ARTIFACTS_DIR = os.environ.get("RAVEWEB_ARTIFACTS_DIR", "")

MIXCLOUD_URL = "https://www.mixcloud.com/djez/nuvolve-046/?utm_source=e2e"
MIXCLOUD_PAGE = "https://www.mixcloud.com/djez/nuvolve-046/"
SOUNDCLOUD_URL = "https://soundcloud.com/dj-example/live-at-kool-fm"


# --------------------------------------------------------------------------- helpers


def _wait_for_rows(page, minimum=1):
    page.wait_for_function(
        "(minimum) => document.querySelectorAll('#list .row:not(.skeleton)').length >= minimum",
        arg=minimum,
    )


def _text(locator):
    """DOM text without the CSS text-transform that inner_text would apply."""
    return (locator.text_content() or "").strip()


def _texts(locator):
    return [value.strip() for value in locator.all_text_contents()]


def _paste(page, value):
    page.fill("#search-input", value)
    page.press("#search-input", "Enter")


def _csp_violations(page):
    return page.evaluate("() => window.__cspViolations || []")


def _widget_violations(page):
    return [
        violation for violation in _csp_violations(page)
        if "mixcloud" in violation["blockedURI"].lower()
        or "soundcloud" in violation["blockedURI"].lower()
    ]


def _wait_for_panel(page):
    page.wait_for_selector("#embed-panel", state="visible")


def _wait_for_widget_stub(page):
    # The route fixture serves this text for framed widget documents, so
    # seeing it proves the browser actually loaded the iframe (not just that
    # the src attribute was set).
    page.frame_locator("#embed-frame").get_by_text("stub widget").wait_for()


def _save_shot(page, name):
    """Save a screenshot when RAVEWEB_ARTIFACTS_DIR is set (report artifacts)."""
    if not ARTIFACTS_DIR:
        return
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    page.screenshot(path=os.path.join(ARTIFACTS_DIR, name + ".png"))


def _shot(browser_page, name):
    _save_shot(browser_page.page, name)


def _delay_fetches(page, needle, delay_ms=600):
    """Client-side delay for one API path (never block Playwright's loop)."""
    page.evaluate("""([needle, delay]) => {
        if (window.__fetchDelayed) return;
        window.__fetchDelayed = true;
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (url.includes(needle)) {
                return new Promise((resolve, reject) => {
                    setTimeout(() => realFetch(input, init).then(resolve, reject), delay);
                });
            }
            return realFetch(input, init);
        };
    }""", [needle, delay_ms])


def _rail_labels(page):
    return _texts(page.locator("#nav button .rail-label"))


RAIL = ["Latest", "Tape Packs", "Pirate Radio", "Mixes & Shows",
        "Live Radio", "Favourites", "Downloads"]


# --------------------------------------------------------------------------- tests


def test_boot_renders_the_shell_and_the_grouped_rail(browser_page):
    page = browser_page.page
    rail = page.evaluate(
        "async () => (await (await fetch('/api/menu')).json()).rail"
    )
    assert [group["label"] for group in rail] == RAIL
    assert page.locator("#nav button").count() == 7
    assert _rail_labels(page) == RAIL
    # Topbar keeps the search; the rail no longer carries a Search button.
    assert page.locator("#search-input").is_visible()
    assert "Search" not in _rail_labels(page)
    assert _text(page.locator("#title")) == "Latest"
    assert _texts(page.locator("#list .row .row-label")) == [
        "Boot Pack", "Boot Episode",
    ]
    # The player footer stays hidden until a track is chosen (no auto-play).
    assert page.locator("#player").count() == 1
    assert page.locator("#audio").count() == 1
    assert "ITEMS" in _text(page.locator("#status"))
    assert browser_page.page_errors == []
    _shot(browser_page, "01-latest-desktop-1280")


def test_section_navigation_renders_rows(browser_page):
    page = browser_page.page
    # The Tape Packs hub now starts with the three literal tape archives.
    page.get_by_role("button", name="Tape Packs", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Tape Packs'")
    _wait_for_rows(page, 7)
    assert _texts(page.locator("#list .row .row-label")) == [
        "Toronto Rave Mixtape Archive", "JungleTapes.com", "RaveTapePacks.com",
        "Classic Pack Series", "By Rave, Venue & Promoter", "By Year",
        "Every Pack",
    ]
    page.locator("#list .row", has_text="Classic Pack Series").click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Quick Filters'")
    # One row per filter in the data file (16 today; again, no hardcoded count).
    filters = page.evaluate(
        "async () => (await (await fetch('/api/filters')).json()).items"
    )
    assert len(filters) >= 15
    _wait_for_rows(page, len(filters))
    assert page.locator("#list .row").count() == len(filters)
    assert _texts(page.locator("#list .row .row-label")) == [
        entry["label"] for entry in filters
    ]
    # The grouped rail stays on the group that owns the open section.
    active = page.locator("#nav button[aria-current='page']")
    assert _text(active.locator(".rail-label")) == "Tape Packs"
    assert page.evaluate("location.hash") == "#/api/filters"
    assert browser_page.page_errors == []


def test_pasted_mixcloud_url_opens_the_embed_panel(browser_page):
    page = browser_page.page
    _paste(page, MIXCLOUD_URL)
    _wait_for_panel(page)

    assert _text(page.locator("#embed-title")) == "Mixcloud set"
    source = page.locator("#embed-frame").get_attribute("src")
    assert source.startswith("https://www.mixcloud.com/widget/iframe/?")
    assert "feed=https%3A%2F%2Fwww.mixcloud.com%2Fdjez%2Fnuvolve-046%2F" in source
    assert _text(page.locator("#embed-external")) == "Open on Mixcloud ↗"
    assert page.locator("#embed-external").get_attribute("href") == MIXCLOUD_PAGE
    assert page.locator("#embed-frame").get_attribute("title") == "Mixcloud player"
    _wait_for_widget_stub(page)
    assert _widget_violations(page) == []
    # Pasting a set URL is not a text search.
    assert "#/api/search" not in page.evaluate("location.hash")

    page.locator("#embed-close").click()
    page.wait_for_selector("#embed-panel", state="hidden")
    assert page.locator("#embed-frame").get_attribute("src") is None


def test_pasted_soundcloud_url_opens_the_panel_and_backdrop_closes_it(browser_page):
    page = browser_page.page
    _paste(page, SOUNDCLOUD_URL)
    _wait_for_panel(page)

    assert _text(page.locator("#embed-title")) == "SoundCloud set"
    source = page.locator("#embed-frame").get_attribute("src")
    assert source.startswith("https://w.soundcloud.com/player/?url=")
    assert _text(page.locator("#embed-external")) == "Open on SoundCloud ↗"
    assert page.locator("#embed-external").get_attribute("href") == SOUNDCLOUD_URL
    assert page.locator("#embed-frame").get_attribute("title") == "SoundCloud player"
    _wait_for_widget_stub(page)
    assert _widget_violations(page) == []

    # Backdrop: click the panel itself, outside the centred card.
    page.locator("#embed-panel").click(position={"x": 8, "y": 8})
    page.wait_for_selector("#embed-panel", state="hidden")
    assert page.locator("#embed-frame").get_attribute("src") is None


def test_normal_text_search_still_navigates(browser_page):
    page = browser_page.page
    _paste(page, "dreamscape")
    page.wait_for_function(
        "() => location.hash.includes('/api/search?q=dreamscape')"
    )
    page.wait_for_function(
        "() => document.querySelector('#title').textContent === 'Search: dreamscape'"
    )
    _wait_for_rows(page, 2)
    assert _texts(page.locator("#list .row .row-label")) == [
        "Dreamscape 10", "Dreamscape 11",
    ]
    assert page.locator("#embed-panel").is_hidden()


def test_csp_allows_the_widget_iframes_and_blocks_other_frames(browser_page):
    page = browser_page.page
    csp = page.evaluate("async () => (await fetch('/')).headers.get('Content-Security-Policy')")
    assert (
        "frame-src https://www.mixcloud.com https://player-widget.mixcloud.com "
        "https://w.soundcloud.com" in csp
    )

    # Both widget hosts load without a CSP refusal (the route fixture serves
    # a stub document, but the frame-src decision is still made for real).
    _paste(page, MIXCLOUD_URL)
    _wait_for_panel(page)
    _wait_for_widget_stub(page)
    page.locator("#embed-close").click()
    page.wait_for_selector("#embed-panel", state="hidden")
    _paste(page, SOUNDCLOUD_URL)
    _wait_for_panel(page)
    _wait_for_widget_stub(page)
    assert _widget_violations(page) == []

    # A frame outside frame-src must be refused: proves the policy is
    # enforced rather than absent.
    page.evaluate(
        """() => {
            const frame = document.createElement("iframe");
            frame.id = "csp-probe";
            frame.src = "/healthz";
            document.body.appendChild(frame);
        }"""
    )
    page.wait_for_function(
        "() => (window.__cspViolations || []).some((v) => v.blockedURI.endsWith('/healthz'))"
    )
    violations = _csp_violations(page)
    assert any(
        violation["directive"].startswith("frame-src")
        and violation["blockedURI"].endswith("/healthz")
        for violation in violations
    ), violations
    page.locator("#embed-close").click()


def test_favourite_add_and_remove_round_trip(browser_page):
    page = browser_page.page
    heart = page.locator("#list .row").first.locator("button[data-fav-key]")
    assert heart.count() == 1
    assert heart.get_attribute("aria-pressed") == "false"

    heart.click()
    page.wait_for_function(
        "() => document.querySelector('#list .row button[data-fav-key]')"
        ".getAttribute('aria-pressed') === 'true'"
    )

    page.get_by_role("button", name="Favourites", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Favourites'")
    _wait_for_rows(page, 1)
    assert _texts(page.locator("#list .row .row-label")) == ["Boot Pack"]

    page.locator("#list .row button[title='Remove from favourites']").click()
    page.wait_for_function("() => document.querySelectorAll('#list .row').length === 0")
    assert page.locator("#empty").is_visible()

def test_pirate_radio_archive_is_reachable_from_the_pirate_radio_door(browser_page):
    """1.6.44: Pirate Radio is a root rail door."""
    page = browser_page.page
    page.get_by_role("button", name="Pirate Radio", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Pirate Radio'")
    _wait_for_rows(page, 3)
    assert _texts(page.locator("#list .row .row-label")) == [
        "Hardscore", "Pirate Radio Archives", "Kool FM Sets",
    ]
    page.locator("#list .row", has_text="Pirate Radio Archives").click()
    page.wait_for_function(
        "() => document.querySelector('#title').textContent === 'Pirate Radio Archives'")
    _wait_for_rows(page, 3)
    assert _texts(page.locator("#list .row .row-label")) == [
        "Risk FM 107.1", "Weekend Rush 92.3", "Flava FM",
    ]
    page.locator("#list .row").first.click()
    page.wait_for_function(
        "() => document.querySelector('#title').textContent === 'Risk FM 107.1'")
    _wait_for_rows(page, 2)
    assert len(_texts(page.locator("#list .row .row-label"))) == 2
    assert browser_page.page_errors == []

# --------------------------------------------------------------------------- 1.6.15 chrome


def test_rail_fits_1440x900_and_favourites_stays_visible(browser_page):
    """The 21-button rail overflowed this viewport and hid the last buttons."""
    page = browser_page.page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    page.wait_for_function(
        "() => document.querySelectorAll('#nav button').length === 7")
    assert _rail_labels(page) == RAIL

    nav = page.evaluate("""() => {
        const rail = document.querySelector('#nav');
        const buttons = [...rail.querySelectorAll('button')];
        const favButton = buttons.find((button) => button.textContent.includes('Favourites'));
        const lastButton = buttons[buttons.length - 1];
        const box = favButton.getBoundingClientRect();
        const lastBox = lastButton.getBoundingClientRect();
        return {
            scrollHeight: rail.scrollHeight, clientHeight: rail.clientHeight,
            top: box.top, bottom: box.bottom, width: box.width, height: box.height,
            lastBottom: lastBox.bottom, lastLabel: lastButton.textContent.trim(),
            innerWidth: window.innerWidth, innerHeight: window.innerHeight,
            docWidth: document.documentElement.scrollWidth,
        };
    }""")
    assert nav["scrollHeight"] <= nav["clientHeight"] + 1, "the rail itself overflows"
    assert nav["bottom"] <= nav["innerHeight"], "Favourites is below the fold"
    assert nav["lastBottom"] <= nav["innerHeight"], "Downloads is below the fold"
    assert nav["lastLabel"].startswith("Downloads")
    assert nav["top"] >= 0 and nav["height"] >= 44
    assert nav["docWidth"] <= nav["innerWidth"] + 1, "page has horizontal overflow"
    _shot(browser_page, "02-rail-1440x900")


def test_back_button_is_large_pinned_and_stays_visible_on_scroll(browser_page):
    page = browser_page.page
    page.get_by_role("button", name="Tape Packs", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Tape Packs'")
    page.locator("#list .row", has_text="Classic Pack Series").click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Quick Filters'")
    page.wait_for_selector("#back", state="visible")

    box = page.locator("#back").bounding_box()
    assert box["height"] >= 44, box
    assert box["width"] >= 96, box
    assert "Back" in _text(page.locator("#back"))
    sticky = page.locator("#back").evaluate("(el) => getComputedStyle(el).position")
    assert sticky == "sticky"

    initial = page.locator("#back").bounding_box()
    page.evaluate("window.scrollTo(0, 700)")
    page.wait_for_timeout(150)
    stuck = page.locator("#back").bounding_box()
    page.evaluate("window.scrollTo(0, 1100)")
    page.wait_for_timeout(150)
    still = page.locator("#back").bounding_box()
    assert page.locator("#back").is_visible()
    assert stuck["y"] < initial["y"] + 1, "the button never pinned"
    assert abs(still["y"] - stuck["y"]) < 2, (stuck, still)
    _shot(browser_page, "03-back-pinned-scrolled")

    page.locator("#back").click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Tape Packs'")
    assert "#/api/rail/packs" in page.evaluate("location.hash")


def test_surprise_me_opens_a_random_pack_or_show(browser_page):
    page = browser_page.page
    assert page.locator("#surprise").is_visible()
    assert "Surprise me" in _text(page.locator("#surprise"))
    page.locator("#surprise").click()
    page.wait_for_function("() => location.hash.startsWith('#/api/item/') || location.hash.startsWith('#/api/podcast/')")
    _wait_for_rows(page, 1)
    assert _text(page.locator("#title")) not in ("", "Latest")
    assert browser_page.page_errors == []
    _shot(browser_page, "04-surprise-result")


def test_phone_rail_and_back_button_are_usable(browser_page):
    page = browser_page.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    assert page.locator("#nav button").count() == 7
    # The rail is a horizontal strip on a phone: it scrolls sideways, not down.
    overflow = page.evaluate("""() => {
        const rail = document.querySelector('#nav');
        return {x: getComputedStyle(rail).overflowX, y: getComputedStyle(rail).overflowY,
                docWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth};
    }""")
    assert overflow["docWidth"] <= overflow["innerWidth"] + 1
    assert overflow["y"] in ("hidden", "clip")

    page.get_by_role("button", name="Tape Packs", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Tape Packs'")
    page.locator("#list .row", has_text="Classic Pack Series").click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Quick Filters'")
    page.wait_for_selector("#back", state="visible")
    box = page.locator("#back").bounding_box()
    assert box["height"] >= 44
    assert box["y"] >= 0
    _shot(browser_page, "05-phone-back-and-rail")

def test_download_for_offline_then_play_and_delete(browser_page):
    """1.6.16: eligible rows can be saved, played from the local copy, deleted."""
    page = browser_page.page
    # Latest: neither row is a direct-file source, so no Download action exists.
    assert page.locator("#list .row .download-btn").count() == 0

    page.locator("#list .row", has_text="Boot Pack").click()
    page.wait_for_function("() => location.hash.includes('/api/item/boot-pack')")
    _wait_for_rows(page, 1)
    download = page.locator("#list .row .download-btn")
    assert download.count() == 1
    download.click()
    page.wait_for_selector("#download-panel", state="visible")
    assert "Boot" in _text(page.locator("#dl-title"))
    assert page.locator("#dl-cancel").is_visible()
    assert page.locator("#dl-size").is_visible()
    assert page.locator("#dl-remaining").is_visible()
    _shot(browser_page, "07-download-panel")

    page.wait_for_function(
        "() => document.querySelector('#toast').textContent.includes('Saved to Downloads')")
    page.wait_for_selector("#download-panel", state="hidden", timeout=5000)

    page.get_by_role("button", name="Downloads", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Downloads'")
    _wait_for_rows(page, 1)
    assert _texts(page.locator("#list .row .row-label")) == ["Boot Track"]
    sub = _text(page.locator("#list .row .row-sub"))
    assert ("KB" in sub or "MB" in sub) and "Internet Archive" in sub
    _shot(browser_page, "06-downloads-view")

    # Offline playback: the player points at the saved copy, not the source.
    page.locator("#list .row").first.click()
    page.wait_for_selector("#player", state="visible")
    source = page.evaluate("() => document.querySelector('#audio').getAttribute('src')")
    assert source.startswith("/api/downloads/") and source.endswith("/stream"), source
    assert browser_page.page_errors == []

    # Delete removes it from the library and the list.
    page.locator("#list .row button[title='Remove download']").click()
    page.wait_for_function("() => document.querySelectorAll('#list .row').length === 0")
    assert page.locator("#empty").is_visible()
    assert page.locator("#player").count() == 1

# --------------------------------------------------------------------------- 1.6.17 spinner pass


def test_loading_pill_waits_150ms_and_clears(browser_page):
    page = browser_page.page
    page.evaluate("""() => {
        window.__pillShows = 0;
        const pill = document.querySelector('#loading-pill');
        new MutationObserver(() => {
            if (!pill.hidden) window.__pillShows += 1;
        }).observe(pill, {attributes: true, attributeFilter: ['hidden']});
    }""")
    # Fast stubbed section: the pill must never appear (150 ms grace).
    page.get_by_role("button", name="Tape Packs", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Tape Packs'")
    assert page.evaluate("window.__pillShows") == 0

    # Slow API: appears after the grace period, then clears on completion.
    _delay_fetches(page, "/api/live", 700)
    page.get_by_role("button", name="Live Radio", exact=True).click()
    page.wait_for_selector("#loading-pill", state="visible", timeout=4000)
    assert page.locator("#loading-pill").get_attribute("aria-live") == "polite"
    _shot(browser_page, "08-loading-pill")
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Live Radio'")
    page.wait_for_selector("#loading-pill", state="hidden", timeout=4000)


def test_download_panel_can_be_cancelled(browser_page):
    page = browser_page.page
    page.locator("#list .row", has_text="Boot Pack").click()
    page.wait_for_function("() => location.hash.includes('/api/item/boot-pack')")
    _wait_for_rows(page, 1)
    page.locator("#list .row .download-btn").first.click()
    page.wait_for_selector("#download-panel", state="visible")
    page.locator("#dl-cancel").click()
    page.wait_for_selector("#download-panel", state="hidden", timeout=5000)
    page.wait_for_function(
        "() => document.querySelector('#toast').textContent.includes('Download cancelled')")

    page.get_by_role("button", name="Downloads", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Downloads'")
    page.wait_for_timeout(400)
    assert page.locator("#list .row").count() == 0
    assert page.locator("#empty").is_visible()
    assert browser_page.page_errors == []


def test_surprise_reveal_spins_then_stamps(browser_page):
    page = browser_page.page
    started = page.evaluate("performance.now()")
    page.locator("#surprise").click()
    page.wait_for_selector("#surprise-reveal", state="visible")
    animation = page.locator("#surprise-reveal .surprise-head").evaluate(
        "(el) => getComputedStyle(el).animationName")
    assert animation != "none", "the reveal head should be animating"
    page.wait_for_function(
        "() => document.querySelector('#surprise-pick').textContent.trim().length > 0")
    _shot(browser_page, "09-surprise-reveal")
    page.wait_for_function(
        "() => location.hash.startsWith('#/api/item/') || location.hash.startsWith('#/api/podcast/')")
    elapsed = page.evaluate("performance.now()") - started
    assert elapsed >= 700, "the reveal must not blink past (%.0f ms)" % elapsed
    assert page.locator("#surprise-reveal").is_hidden()
    assert browser_page.page_errors == []


def test_reduced_motion_skips_spinner_animation_and_surprise_delay(browser_page):
    page = browser_page.page
    page.emulate_media(reduced_motion="reduce")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.querySelectorAll('#list .row:not(.skeleton)').length > 0")

    animation = page.locator("#surprise-reveal .surprise-head").evaluate(
        "(el) => getComputedStyle(el).animationName")
    assert animation == "none"

    page.evaluate("""() => {
        window.__revealShows = 0;
        const reveal = document.querySelector('#surprise-reveal');
        new MutationObserver(() => {
            if (!reveal.hidden) window.__revealShows += 1;
        }).observe(reveal, {attributes: true, attributeFilter: ['hidden']});
    }""")
    started = page.evaluate("performance.now()")
    page.locator("#surprise").click()
    page.wait_for_function(
        "() => location.hash.startsWith('#/api/item/') || location.hash.startsWith('#/api/podcast/')")
    elapsed = page.evaluate("performance.now()") - started
    assert page.evaluate("window.__revealShows") == 0, "reduced motion must skip the reveal"
    # The decisive check is that the animated reveal never appeared; this upper
    # bound only catches a future re-introduced 900 ms spin wait (it is loose
    # enough for a loaded CI/desktop machine).
    assert elapsed < 2500, "reduced motion must not wait for the reveal (%.0f ms)" % elapsed
    assert page.locator("#surprise-reveal").is_hidden()

    # The loading pill still reports, just without animation.
    _delay_fetches(page, "/api/rail/pirate", 600)
    page.get_by_role("button", name="Pirate Radio", exact=True).click()
    page.wait_for_selector("#loading-pill", state="visible", timeout=4000)
    pill_animation = page.locator("#loading-pill .spinner-head").evaluate(
        "(el) => getComputedStyle(el).animationName")
    assert pill_animation == "none"
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Pirate Radio'")
    page.wait_for_selector("#loading-pill", state="hidden", timeout=4000)

def test_pwa_installs_and_any_same_origin_link_opens_the_app(browser_page, live_server):
    """1.6.19: manifest identity + SW navigation fallback for home-screen use."""
    page = browser_page.page
    manifest = page.evaluate(
        "async () => (await (await fetch('/manifest.webmanifest')).json())")
    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert page.evaluate(
        "() => document.querySelector('meta[name=\"apple-mobile-web-app-capable\"]').content") == "yes"

    assert page.evaluate("() => 'serviceWorker' in navigator && window.isSecureContext")
    scope = page.evaluate(
        "async () => { const reg = await navigator.serviceWorker.ready; return reg.scope; }")
    assert scope.rstrip("/") == live_server.url.rstrip("/")

    # The worker must be controlling the page before we test the fallback.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => navigator.serviceWorker.controller !== null", timeout=15000)

    # A stale/foreign-looking same-origin link (what an old bookmark may hold)
    # still boots the app shell instead of a server 404 or a stale page.
    page.goto(live_server.url + "/stale-ad-or-any-link", wait_until="domcontentloaded")
    page.wait_for_selector("#nav button", timeout=15000)
    assert page.evaluate("location.pathname") == "/stale-ad-or-any-link"
    assert page.locator("#title").is_visible()

    # Live data is never served from the worker cache.
    assert page.evaluate("fetch('/api/menu').then((response) => response.status)") == 200
    assert browser_page.page_errors == []


def test_login_logout_flow_and_login_page_design(browser_page, live_server):
    """1.6.20: the door, failed login, reveal, successful login, logout."""
    page = browser_page.page
    assert page.locator("#logout").is_visible()

    page.locator("#logout").click()
    page.wait_for_selector("#login-form")
    assert page.locator("#login-title").text_content().strip() == "THE DOOR"
    assert page.locator("#login-form").is_visible()
    _shot(browser_page, "login-desktop")

    # Password reveal is a real toggle and keeps its accessible state.
    page.fill("#username", "owner")
    page.fill("#password", "not-the-password")
    page.click("#password-toggle")
    assert page.get_attribute("#password", "type") == "text"
    assert page.get_attribute("#password-toggle", "aria-pressed") == "true"
    page.click("#password-toggle")
    assert page.get_attribute("#password", "type") == "password"

    # Generic failure, no username/password echo.
    page.click("#login-submit")
    page.wait_for_selector(".door-error")
    assert "invalid username or password." in page.locator(".door-error").inner_text().lower()
    assert "not-the-password" not in page.content()

    # Phone layout (single column, 44px targets) in a fresh context, so the
    # screenshot is not affected by Chromium's viewport-resize compositor.
    phone_context = page.context.browser.new_context(
        viewport={"width": 390, "height": 844})
    try:
        phone = phone_context.new_page()
        phone.goto(live_server.url + "/login", wait_until="domcontentloaded")
        phone.wait_for_selector("#login-form")
        assert phone.locator(".door").count() == 1
        assert phone.locator("#login-submit").bounding_box()["height"] >= 44
        assert phone.locator("#password-toggle").bounding_box()["height"] >= 44
        _save_shot(phone, "login-phone")
    finally:
        phone_context.close()

    # Remember-me + correct password open the app.
    page.fill("#username", "owner")
    page.fill("#password", "test-password")
    page.check("#remember")
    page.click("#login-submit")
    page.wait_for_selector("#nav button")
    page.wait_for_function(
        "() => document.querySelectorAll('#list .row:not(.skeleton)').length > 0")
    assert page.locator("#logout").is_visible()
    assert browser_page.page_errors == []


def test_background_download_survives_navigation_and_completes(browser_page):
    """1.6.23: background job keeps running when the tab leaves the panel."""
    from test_e2e import conftest as e2e_conftest
    page = browser_page.page
    old_delay = e2e_conftest._FakeDownloadResponse.DELAY
    e2e_conftest._FakeDownloadResponse.DELAY = 0.12  # ~3.9 s: enough to navigate
    try:
        page.locator("#list .row", has_text="Boot Pack").click()
        page.wait_for_function("() => location.hash.includes('/api/item/boot-pack')")
        _wait_for_rows(page, 1)
        page.locator("#list .row .download-btn").first.click()
        page.wait_for_selector("#download-panel", state="visible")
        assert page.locator("#dl-background").is_visible()
        page.locator("#dl-background").click()
        page.wait_for_selector("#download-panel", state="hidden", timeout=5000)
        page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('background')",
            timeout=5000)

        # Navigate away while the server-side job is still running.
        page.get_by_role("button", name="Latest", exact=True).click()
        page.wait_for_function("() => document.querySelector('#title').textContent === 'Latest'")
        # It must also survive a full page reload, not just client navigation.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#nav button")
        page.wait_for_selector("#job-chip", state="visible", timeout=5000)
        assert "%" in page.locator("#job-chip-text").text_content() or \
            "QUEUED" in page.locator("#job-chip-text").text_content().upper()

        # Completion is detected while elsewhere, then the row is in Downloads.
        page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('Saved to Downloads')",
            timeout=15000)
        page.get_by_role("button", name="Downloads", exact=True).click()
        page.wait_for_function("() => document.querySelector('#title').textContent === 'Downloads'")
        _wait_for_rows(page, 1)
        page.wait_for_function(
            "() => document.querySelectorAll('#list .job-row').length === 0", timeout=8000)
        assert page.locator("#list .row .row-label").first.text_content().strip() == "Boot Track"

        # Clean up so later tests see an empty Downloads view.
        page.locator("#list .row button[title='Remove download']").click()
        page.wait_for_function("() => document.querySelectorAll('#list .row').length === 0")
        assert browser_page.page_errors == []
    finally:
        e2e_conftest._FakeDownloadResponse.DELAY = old_delay


def test_themed_404_and_csp_reporting(browser_page, live_server):
    page = browser_page.page
    # Fetch rather than navigate: the app-shell service worker intentionally
    # turns stale same-origin navigations into the cached app, so this checks
    # the server's 404 document/headers directly.
    result = page.evaluate("""async () => {
        const response = await fetch('/no-such-reel');
        return {status: response.status, text: await response.text(),
                csp: response.headers.get('content-security-policy-report-only') || ''};
    }""")
    assert result["status"] == 404
    assert "LOST THE TAPE" in result["text"]
    assert 'href="/"' in result["text"]
    assert "report-uri /api/csp-report" in result["csp"]
    # API 404s stay JSON.
    api = page.evaluate("async () => { const r = await fetch('/api/no-such-reel'); return {status:r.status, json:await r.json()}; }")
    assert api["status"] == 404 and api["json"]["error"] == "not found"
    # Browser report endpoint is public and accepts a report without a session.
    status = page.evaluate("""async () => {
        const r = await fetch('/api/csp-report', {method: 'POST',
            headers: {'Content-Type': 'application/csp-report'},
            body: JSON.stringify({'csp-report': {'effective-directive': 'img-src',
                'blocked-uri': 'https://example.invalid/x.png',
                'document-uri': location.href, 'disposition': 'report'}})});
        return r.status;
    }""")
    assert status == 204
    assert browser_page.page_errors == []


def test_mobile_search_field_fits_and_submit_is_tappable(browser_page):
    """1.6.28: 390px iPhone search row must not overflow or cover submit."""
    page = browser_page.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    box = page.evaluate("""() => {
        const search = document.querySelector('.search').getBoundingClientRect();
        const field = document.querySelector('.search-field').getBoundingClientRect();
        const input = document.querySelector('#search-input').getBoundingClientRect();
        const button = document.querySelector('.search button').getBoundingClientRect();
        const hit = document.elementFromPoint(button.left + button.width / 2,
                                              button.top + button.height / 2);
        return {
            innerWidth: window.innerWidth,
            docWidth: document.documentElement.scrollWidth,
            search: {left: search.left, right: search.right, width: search.width},
            field: {left: field.left, right: field.right, width: field.width},
            input: {left: input.left, right: input.right, width: input.width},
            button: {left: button.left, right: button.right, width: button.width,
                     height: button.height},
            hit: hit ? hit.closest('button') ? 'button' : hit.tagName : 'none',
        };
    }""")
    assert box["docWidth"] <= box["innerWidth"] + 1, box
    assert box["input"]["right"] <= box["field"]["right"] + 1, box
    assert box["input"]["left"] >= box["field"]["left"] - 1, box
    assert box["input"]["width"] >= 120, box
    assert box["button"]["width"] >= 44 and box["button"]["height"] >= 44, box
    assert box["button"]["left"] >= box["field"]["right"] - 1, box
    assert box["button"]["right"] <= box["search"]["right"] + 1, box
    assert box["hit"] == "button", box
    assert page.locator("#search-input").is_visible()
    assert browser_page.page_errors == []


# --------------------------------------------------------------------------- 1.6.31


def _set_fake_download_delay(seconds):
    """Patch every loaded copy of the e2e fake download response class.

    ``from test_e2e import conftest`` can resolve to a different module object
    than the one pytest loaded for the fixture, so patching a single import is
    not reliable. Walking ``sys.modules`` catches all copies.
    """
    import sys

    patched = []
    for module in list(sys.modules.values()):
        cls = getattr(module, "_FakeDownloadResponse", None)
        if cls is not None and hasattr(cls, "DELAY"):
            patched.append((cls, cls.DELAY))
            cls.DELAY = seconds
    return patched


def test_single_topbar_spinner_during_background_download(browser_page):
    """1.6.31: the download chip owns the top-right indicator — one face max."""
    page = browser_page.page
    patched = _set_fake_download_delay(0.4)  # ~13 s: outlives the sample window
    assert patched, "the e2e fake download class is not loaded"
    try:
        # Queue a real server-side job through the app's own endpoint, with a
        # unique URL so a leftover library entry cannot turn it into "already".
        unique = "spinner-%d.mp3" % int(time.time() * 1000)
        created = page.evaluate("""async (name) => {
            const response = await fetch('/api/downloads/jobs', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    url: 'https://archive.org/download/boot-pack/' + name,
                    title: 'Spinner Test',
                    source: 'archive',
                }),
            });
            return {status: response.status, body: await response.json()};
        }""", unique)
        assert created["status"] == 202, created

        # Reload: boot's job poll adopts the running job and starts its 1.6 s
        # loop, which is what used to raise the pill alongside the chip.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#nav button")
        page.wait_for_selector("#job-chip", state="visible", timeout=10000)

        # Sample the topbar continuously across several 1.6 s job polls.
        page.evaluate("""() => {
            const visible = (el) => !!(el && !el.hidden);
            const sample = () => {
                const pill = visible(document.querySelector('#loading-pill'));
                const chip = visible(document.querySelector('#job-chip'));
                // Each surface holds exactly one head; count the *surfaces*
                // whose hidden state makes that head visible.
                const heads = (pill ? 1 : 0) + (chip ? 1 : 0);
                window.__spinner = window.__spinner || {pill: 0, chip: 0, both: 0, max: 0};
                if (pill) window.__spinner.pill += 1;
                if (chip) window.__spinner.chip += 1;
                if (pill && chip) window.__spinner.both += 1;
                window.__spinner.max = Math.max(window.__spinner.max, heads);
            };
            window.__spinner = {pill: 0, chip: 0, both: 0, max: 0};
            sample();
            window.__spinnerTimer = setInterval(sample, 50);
        }""")
        page.wait_for_timeout(2600)
        counts = page.evaluate(
            "() => { clearInterval(window.__spinnerTimer); return window.__spinner; }")
        assert counts["max"] == 1, counts
        assert counts["both"] == 0, counts
        assert counts["chip"] > 0, counts
        assert counts["pill"] == 0, counts
        assert page.locator("#loading-pill").is_hidden()
        _shot(browser_page, "10-single-spinner")

        # A non-download API load still gets the pill once the job is done.
        page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('Saved to Downloads')",
            timeout=30000)
        page.wait_for_selector("#job-chip", state="hidden", timeout=10000)
        _delay_fetches(page, "/api/live", 700)
        page.get_by_role("button", name="Live Radio", exact=True).click()
        page.wait_for_selector("#loading-pill", state="visible", timeout=4000)
        assert page.evaluate("""() => {
            const pill = document.querySelector('#loading-pill');
            const chip = document.querySelector('#job-chip');
            return !!(pill && !pill.hidden) && !!(!chip || chip.hidden);
        }""")
        page.wait_for_selector("#loading-pill", state="hidden", timeout=6000)

        # Leave the shared Downloads library empty for later tests.
        page.get_by_role("button", name="Downloads", exact=True).click()
        page.wait_for_function("() => document.querySelector('#title').textContent === 'Downloads'")
        page.wait_for_function(
            "() => document.querySelectorAll('#list .row').length > 0", timeout=8000)
        page.locator("#list .row button[title='Remove download']").click()
        page.wait_for_function("() => document.querySelectorAll('#list .row').length === 0")
        assert browser_page.page_errors == []
    finally:
        for cls, delay in patched:
            cls.DELAY = delay


def test_debug_panel_is_absent_unless_enabled(browser_page, live_server):
    """1.6.31: no panel, no capture and no localStorage write unless opted in."""
    page = browser_page.page
    assert page.locator("#debug-panel").count() == 0
    assert page.evaluate("() => window.RaveDebug.enabled") is False
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") is None

    # The stored flag turns it on across reloads.
    page.evaluate("() => localStorage.setItem('raveplayer.debug', '1')")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#debug-panel", state="visible")

    # ?debug=0 clears both the page state and the stored flag.
    page.goto(live_server.url + "/?debug=0", wait_until="domcontentloaded")
    assert page.locator("#debug-panel").count() == 0
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") is None
    assert browser_page.page_errors == []


def test_debug_panel_records_exports_and_clears(browser_page, live_server, tmp_path):
    """1.6.31: ?debug=1 gives a local ring buffer with copy/download/clear."""
    page = browser_page.page
    page.goto(live_server.url + "/?debug=1", wait_until="domcontentloaded")
    page.wait_for_selector("#debug-panel", state="visible")
    page.wait_for_function(
        "() => document.querySelectorAll('#list .row:not(.skeleton)').length > 0")
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") == "1"
    assert page.locator("#debug-panel .debug-title").text_content().strip() == "DEBUG"

    # Boot already recorded the route, the API calls and the service worker.
    page.wait_for_function("""() => {
        const kinds = Array.from(document.querySelectorAll('#debug-log .debug-kind'))
            .map((el) => el.textContent);
        return kinds.includes('route') && kinds.includes('api');
    }""")
    assert "api" in page.evaluate(
        "() => window.RaveDebug.snapshot().events.map((e) => e.kind).join(',')")
    api_event = page.evaluate("""() => {
        const event = window.RaveDebug.snapshot().events.find((e) => e.kind === 'api');
        return event ? event.data : null;
    }""")
    assert api_event["path"].startswith("/api/")
    assert api_event["method"] == "GET"
    assert api_event["status"] == 200
    assert isinstance(api_event["ms"], int)
    assert api_event["requestId"]

    # Media events are captured, and redaction happens before storage.
    page.evaluate("() => document.querySelector('#audio').dispatchEvent(new Event('play'))")
    page.evaluate("""() => window.RaveDebug.record('probe', {
        password: 'hunter2', token: 'tok-abc', note: 'keep-me'})""")
    page.wait_for_function(
        "() => document.querySelectorAll('#debug-log .debug-line').length > 0")
    text = page.evaluate("() => window.RaveDebug.formatText()")
    assert '"kind":"media"' in text
    assert "hunter2" not in text and "tok-abc" not in text
    assert "[redacted]" in text and "keep-me" in text

    # Clear empties both the buffer and the rendered list.
    page.locator('#debug-panel .debug-btn[data-act="clear"]').click()
    page.wait_for_function("() => window.RaveDebug.size === 0")
    assert page.locator("#debug-log li").count() == 0
    assert page.locator("#debug-panel").is_visible()

    # Generate a fresh event, then Copy (clipboard stubbed so the test is
    # deterministic in headless Chromium).
    page.evaluate("() => window.RaveDebug.record('probe', { note: 'copy-me' })")
    page.evaluate("""() => {
        window.__copied = '';
        Object.defineProperty(navigator.clipboard, 'writeText', {
            configurable: true,
            value: (value) => { window.__copied = String(value); return Promise.resolve(); },
        });
    }""")
    page.locator('#debug-panel .debug-btn[data-act="copy"]').click()
    page.wait_for_function("() => (window.__copied || '').includes('copy-me')")
    assert '"kind":"probe"' in page.evaluate("() => window.__copied")
    assert page.locator('#debug-panel .debug-btn[data-act="copy"]').text_content().strip() == "Copied"

    # Download JSON writes a local file with the request id and the redactions.
    with page.expect_download() as download_info:
        page.locator('#debug-panel .debug-btn[data-act="download"]').click()
    target = tmp_path / "rave-debug.json"
    download_info.value.save_as(str(target))
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["kind"] == "rave-debug-log"
    assert payload["maxEvents"] == 500
    assert payload["events"]
    kinds = {event["kind"] for event in payload["events"]}
    assert "probe" in kinds
    assert "hunter2" not in target.read_text(encoding="utf-8")
    _shot(browser_page, "11-debug-panel")
    assert browser_page.page_errors == []


def test_debug_menu_toggle_turns_panel_on_and_off(browser_page, live_server):
    """1.6.32: the Help switch is the same toggle as ?debug=1/?debug=0."""
    page = browser_page.page
    toggle = page.locator("#debug-toggle")
    assert page.locator("#debug-panel").count() == 0
    assert page.evaluate("() => window.RaveDebug.enabled") is False
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") is None

    # The switch lives in the existing Help/utility dialog and starts off.
    page.click("#help-btn")
    assert page.locator("#help").is_visible()
    assert _text(toggle) == "Debug log: off"
    assert toggle.get_attribute("aria-pressed") == "false"
    assert "Debug log: off" in (toggle.get_attribute("aria-label") or "")

    # Keyboard support: focus the real button, press Enter (not a mouse click).
    toggle.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#debug-panel", state="visible")
    assert _text(toggle) == "Debug log: on"
    assert toggle.get_attribute("aria-pressed") == "true"
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") == "1"
    _shot(browser_page, "12-debug-toggle-on")

    # Escape closes the dialog only; capture keeps running.
    page.keyboard.press("Escape")
    assert page.locator("#help").is_hidden()
    assert page.locator("#debug-panel").is_visible()
    page.evaluate("() => window.RaveDebug.record('probe', { note: 'before-reload' })")

    # Reload: the stored flag reopens the panel and the switch reflects it.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#debug-panel", state="visible")
    assert page.evaluate("() => window.RaveDebug.enabled") is True
    page.click("#help-btn")
    assert _text(toggle) == "Debug log: on"
    assert toggle.get_attribute("aria-pressed") == "true"

    # Keyboard support again: Space turns it off, clearing flag and buffer.
    toggle.focus()
    page.keyboard.press("Space")
    page.wait_for_selector("#debug-panel", state="detached")
    assert _text(toggle) == "Debug log: off"
    assert toggle.get_attribute("aria-pressed") == "false"
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") is None
    assert page.evaluate("() => window.RaveDebug.size") == 0
    assert page.evaluate(
        "() => window.RaveDebug.record('probe', { after: 1 })") is None
    _shot(browser_page, "13-debug-toggle-off")

    # Reload: off sticks and the button still shows the off state.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => window.RaveDebug && window.RaveDebug.enabled === false")
    assert page.locator("#debug-panel").count() == 0
    page.click("#help-btn")
    assert _text(toggle) == "Debug log: off"

    # 44 px mobile target, inside the existing dialog (no new nav row).
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    page.click("#help-btn")
    assert toggle.is_visible()
    box = toggle.bounding_box()
    assert box is not None
    assert box["height"] >= 44, box
    assert box["width"] <= 390, box
    _shot(browser_page, "14-debug-toggle-mobile")

    # The URL switch still works and updates the button on load.
    page.goto(live_server.url + "/?debug=1", wait_until="domcontentloaded")
    page.wait_for_selector("#debug-panel", state="visible")
    page.click("#help-btn")
    assert _text(toggle) == "Debug log: on"
    assert toggle.get_attribute("aria-pressed") == "true"
    assert page.evaluate("() => localStorage.getItem('raveplayer.debug')") == "1"
    assert browser_page.page_errors == []


def test_help_dialog_is_themed_and_mobile_safe(browser_page):
    """1.6.33: Help button/dialog use the brutalist tokens at phone size."""
    page = browser_page.page
    button = page.locator("#help-btn")
    assert button.is_visible()
    button.click()
    page.wait_for_selector("#help", state="visible")
    assert page.locator(".help-card").is_visible()
    # The debug block and toggle must stay readable/prominent.
    assert page.locator(".help-debug").is_visible()
    toggle = page.locator("#debug-toggle")
    assert toggle.is_visible()
    assert toggle.bounding_box()["height"] >= 44
    style = page.evaluate("""() => {
        const btn = getComputedStyle(document.querySelector('#help-btn'));
        const card = getComputedStyle(document.querySelector('.help-card'));
        const debug = getComputedStyle(document.querySelector('.help-debug'));
        const toggle = getComputedStyle(document.querySelector('#debug-toggle'));
        return {
            btnRadius: btn.borderRadius, btnShadow: btn.boxShadow,
            btnSize: [btn.width, btn.height],
            cardRadius: card.borderRadius, cardShadow: card.boxShadow,
            debugBorder: debug.borderTopColor, toggleRadius: toggle.borderRadius,
            toggleShadow: toggle.boxShadow,
        };
    }""")
    assert style["btnRadius"] == "0px" and style["cardRadius"] == "0px"
    assert style["btnShadow"] != "none" and style["cardShadow"] != "none"
    assert style["toggleRadius"] == "0px" and style["toggleShadow"] != "none"
    assert "232, 255, 0" in style["debugBorder"] or "34, 230, 255" in style["debugBorder"]
    _shot(browser_page, "help-desktop")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(100)
    help_box = page.locator("#help").bounding_box()
    assert help_box["x"] >= 0 and help_box["y"] >= 0
    assert help_box["width"] <= 390 and help_box["height"] <= 844
    card = page.locator(".help-card").bounding_box()
    assert card["width"] <= 390 and card["x"] >= 0
    assert page.locator("#help-close").bounding_box()["height"] >= 44
    _shot(browser_page, "help-phone")
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")
    assert browser_page.page_errors == []


# --------------------------------------------------------------------------- 1.6.37 appearance


def _open_help(page):
    page.click("#help-btn")
    page.wait_for_selector("#help", state="visible")


def _choose_appearance(page, label, mode):
    page.locator(".appearance-option", has_text=label).click()
    page.wait_for_function(
        "(mode) => document.documentElement.dataset.themeMode === mode",
        arg=mode,
    )


def _theme_state(page):
    return page.evaluate("""() => ({
        theme: document.documentElement.dataset.theme,
        mode: document.documentElement.dataset.themeMode,
        stored: localStorage.getItem('raveplayer.theme'),
        bg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
        text: getComputedStyle(document.body).color,
        card: getComputedStyle(document.querySelector('.help-card')).backgroundColor,
        meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
    })""")


def test_appearance_light_and_dark_persist_with_screenshots(browser_page):
    """1.6.35: Dark/Light in the Help dialog, persisted, with review shots."""
    page = browser_page.page
    state = page.evaluate("""() => ({
        theme: document.documentElement.dataset.theme,
        mode: document.documentElement.dataset.themeMode,
        stored: localStorage.getItem('raveplayer.theme'),
    })""")
    assert state == {"theme": "dark", "mode": "dark", "stored": None}
    _open_help(page)
    assert page.locator('input[name="appearance"]').count() == 3
    assert page.locator(".appearance-option.is-checked").inner_text().strip() == "DARK"

    _choose_appearance(page, "Light", "light")
    light = _theme_state(page)
    assert light["theme"] == "light" and light["mode"] == "light"
    assert light["stored"] == "light"
    assert light["bg"] == "#f2eee3"
    assert light["text"] == "rgb(7, 7, 11)"
    assert light["meta"] == "#f2eee3"
    assert light["card"] == "rgb(251, 248, 239)"
    # The selected option is a real radio, 44 px and visibly checked.
    selected = page.locator(".appearance-option.is-checked")
    assert _text(selected) == "Light"
    assert selected.bounding_box()["height"] >= 44
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")
    _shot(browser_page, "16-theme-light-desktop")

    # Reload: the blocking head script applies Light before the CSS paints.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    reloaded = page.evaluate("""() => ({
        theme: document.documentElement.dataset.theme,
        mode: document.documentElement.dataset.themeMode,
        stored: localStorage.getItem('raveplayer.theme'),
    })""")
    assert reloaded == {"theme": "light", "mode": "light", "stored": "light"}

    _open_help(page)
    _choose_appearance(page, "Dark", "dark")
    dark = _theme_state(page)
    assert dark["theme"] == "dark" and dark["mode"] == "dark"
    assert dark["bg"] == "#07070b"
    assert dark["text"] == "rgb(242, 238, 227)"
    assert dark["meta"] == "#0a0a0f"
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")
    _shot(browser_page, "15-theme-dark-desktop")

    # Phone: both themes, 390 px, no horizontal overflow.
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(100)
    _shot(browser_page, "17-theme-dark-phone")
    _open_help(page)
    _choose_appearance(page, "Light", "light")
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")
    overflow = page.evaluate("""() => ({
        docWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        bg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
        text: getComputedStyle(document.body).color,
    })""")
    assert overflow["docWidth"] <= overflow["innerWidth"] + 1
    assert overflow["bg"] == "#f2eee3"
    assert overflow["text"] == "rgb(7, 7, 11)"
    _shot(browser_page, "18-theme-light-phone")
    assert browser_page.page_errors == []


def test_appearance_system_follows_prefers_color_scheme(browser_page):
    page = browser_page.page
    page.emulate_media(color_scheme="light")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    _open_help(page)
    _choose_appearance(page, "System", "system")
    assert page.evaluate("() => localStorage.getItem('raveplayer.theme')") == "system"
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'light'"
    )
    assert _theme_state(page)["mode"] == "system"
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")

    # The OS changes while the page is open: System follows it live.
    page.emulate_media(color_scheme="dark")
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'dark'"
    )
    page.emulate_media(color_scheme="light")
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'light'"
    )

    # An explicit Dark choice stops following the OS.
    _open_help(page)
    _choose_appearance(page, "Dark", "dark")
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")
    page.emulate_media(color_scheme="dark")
    page.emulate_media(color_scheme="light")
    page.wait_for_timeout(150)
    assert page.evaluate("() => document.documentElement.dataset.theme") == "dark"

    # And System survives a reload, following the (now dark) device.
    _open_help(page)
    _choose_appearance(page, "System", "system")
    page.locator("#help-close").click()
    page.emulate_media(color_scheme="dark")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    assert page.evaluate("() => document.documentElement.dataset.themeMode") == "system"
    assert page.evaluate("() => document.documentElement.dataset.theme") == "dark"
    assert page.evaluate("() => localStorage.getItem('raveplayer.theme')") == "system"
    assert browser_page.page_errors == []


def test_appearance_is_mobile_safe_and_keyboard_focus_visible(browser_page):
    page = browser_page.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#nav button")
    _open_help(page)
    _choose_appearance(page, "Light", "light")

    boxes = page.locator(".appearance-option").evaluate_all(
        "els => els.map(el => { const b = el.getBoundingClientRect();"
        " return {w: b.width, h: b.height, x: b.x, right: b.right}; })"
    )
    assert len(boxes) == 3
    for box in boxes:
        assert box["h"] >= 44, box
        assert box["w"] > 80, box
        assert box["x"] >= 0 and box["right"] <= 390, box
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
    )

    # Keyboard focus lands on the real radio and the label draws the ring.
    page.locator('input[name="appearance"][value="system"]').focus()
    focus = page.evaluate("""() => {
        const input = document.querySelector('input[name="appearance"][value="system"]');
        const label = input.closest('.appearance-option');
        const style = getComputedStyle(label);
        return {
            focused: document.activeElement === input,
            outlineStyle: style.outlineStyle,
            outlineWidth: style.outlineWidth,
            outlineColor: style.outlineColor,
        };
    }""")
    assert focus["focused"] is True
    assert focus["outlineStyle"] == "solid"
    assert focus["outlineWidth"] == "2px"
    assert focus["outlineColor"] == "rgb(7, 7, 11)"

    # The debug switch still exists beside it and keeps its 44 px target.
    assert page.locator("#debug-toggle").is_visible()
    assert page.locator("#debug-toggle").bounding_box()["height"] >= 44
    _shot(browser_page, "19-theme-light-help-phone")
    page.locator("#help-close").click()
    page.wait_for_selector("#help", state="hidden")
    assert browser_page.page_errors == []


def test_taxonomy_rail_and_tape_packs_in_both_themes(browser_page):
    """1.6.44: the scene taxonomy reads the same in dark and light."""
    page = browser_page.page
    for mode in ("dark", "light"):
        _open_help(page)
        _choose_appearance(page, "Light" if mode == "light" else "Dark", mode)
        page.locator("#help-close").click()
        page.wait_for_selector("#help", state="hidden")
        assert _rail_labels(page) == [
            "Latest", "Tape Packs", "Pirate Radio", "Mixes & Shows",
            "Live Radio", "Favourites", "Downloads",
        ]
        page.get_by_role("button", name="Latest", exact=True).click()
        page.wait_for_function(
            "() => document.querySelector('#title').textContent === 'Latest'")
        page.get_by_role("button", name="Tape Packs", exact=True).click()
        page.wait_for_function(
            "() => document.querySelector('#title').textContent === 'Tape Packs'")
        _wait_for_rows(page, 7)
        assert _texts(page.locator("#list .row .row-label")) == [
            "Toronto Rave Mixtape Archive", "JungleTapes.com",
            "RaveTapePacks.com", "Classic Pack Series",
            "By Rave, Venue & Promoter", "By Year", "Every Pack",
        ]
        _shot(browser_page, "20-taxonomy-tape-packs-%s" % mode)
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert browser_page.page_errors == []
    # Leave the default dark choice for later tests.
    _open_help(page)
    _choose_appearance(page, "Dark", "dark")
    page.locator("#help-close").click()
