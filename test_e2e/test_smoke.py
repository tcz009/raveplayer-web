# -*- coding: utf-8 -*-
"""Headless-browser smoke tests: the class of bugs the offline suite missed.

Covers page boot + the grouped 8-button rail (1.6.15; derived from
`/api/menu.rail`), hub navigation and section rows, the pinned back button,
Surprise Me, the pasted Mixcloud/SoundCloud embed panel (widget src, provider
label, close and backdrop behaviour, CSP not refusing the iframes), normal text
search, and a favourites add/remove round trip.  Set ``RAVEWEB_ARTIFACTS_DIR``
to also write the before/after screenshots.

These are deterministic and offline: the Flask app is started locally by the
shared fixtures and every provider call is stubbed (see conftest.py).  Run
with ``pytest test_e2e``; they are not part of ``pytest test``.
"""

import os

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


def _shot(browser_page, name):
    """Save a screenshot when RAVEWEB_ARTIFACTS_DIR is set (report artifacts)."""
    if not ARTIFACTS_DIR:
        return
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    browser_page.page.screenshot(path=os.path.join(ARTIFACTS_DIR, name + ".png"))


def _rail_labels(page):
    return _texts(page.locator("#nav button .rail-label"))


RAIL = ["Latest", "Tape Packs", "Mixes & Shows", "Club & Rave Sets",
        "SoundCloud Picks", "Live Radio", "Curated Gems", "Favourites",
        "Downloads"]


# --------------------------------------------------------------------------- tests


def test_boot_renders_the_shell_and_the_grouped_rail(browser_page):
    page = browser_page.page
    rail = page.evaluate(
        "async () => (await (await fetch('/api/menu')).json()).rail"
    )
    assert [group["label"] for group in rail] == RAIL
    assert page.locator("#nav button").count() == 9
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
    # The rail groups sections: Quick Filters lives in the Tape Packs hub.
    page.get_by_role("button", name="Tape Packs", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Tape Packs'")
    _wait_for_rows(page, 3)
    assert _texts(page.locator("#list .row .row-label")) == [
        "Tape Packs", "By Year", "Quick Filters",
    ]
    page.locator("#list .row", has_text="Quick Filters").click()
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

def test_pirate_radio_archive_is_reachable_from_curated_gems(browser_page):
    """1.6.15: Curated Gems is the hub row that owns Pirate Radio Archives."""
    page = browser_page.page
    page.get_by_role("button", name="Curated Gems", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Curated Gems'")
    _wait_for_rows(page, 2)
    assert _texts(page.locator("#list .row .row-label")) == [
        "Pirate Radio Archives", "Legacy Sites",
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
        "() => document.querySelectorAll('#nav button').length === 9")
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
    page.locator("#list .row", has_text="Quick Filters").click()
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
    assert page.locator("#nav button").count() == 9
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
    page.locator("#list .row", has_text="Quick Filters").click()
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
    page.wait_for_function(
        "() => document.querySelector('#toast').textContent.includes('Saved to Downloads')")

    page.get_by_role("button", name="Downloads", exact=True).click()
    page.wait_for_function("() => document.querySelector('#title').textContent === 'Downloads'")
    _wait_for_rows(page, 1)
    assert _texts(page.locator("#list .row .row-label")) == ["Boot Track"]
    sub = _text(page.locator("#list .row .row-sub"))
    assert "KB" in sub and "Internet Archive" in sub
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
