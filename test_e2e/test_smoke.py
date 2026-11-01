# -*- coding: utf-8 -*-
"""Headless-browser smoke tests: the class of bugs the offline suite missed.

Covers page boot + the full section menu (currently 21 entries; derived from
`/api/menu` so adding a section updates one data file, not this test), section
navigation rendering every row, the pasted Mixcloud/SoundCloud embed panel
(widget src, provider label, close and backdrop behaviour, CSP not refusing the
iframes), normal text search, and a favourites add/remove round trip.

These are deterministic and offline: the Flask app is started locally by the
shared fixtures and every provider call is stubbed (see conftest.py).  Run
with ``pytest test_e2e``; they are not part of ``pytest test``.
"""

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


# --------------------------------------------------------------------------- tests


def test_boot_renders_the_shell_and_all_sections(browser_page):
    page = browser_page.page
    # The nav must render exactly the sections the API advertises.  Deriving the
    # count keeps this test honest when a section is added (Rinse FM arrived in
    # 1.6.10) instead of failing on a stale hardcoded number.
    sections = page.evaluate(
        "async () => (await (await fetch('/api/menu')).json()).sections"
    )
    assert len(sections) >= 15
    assert page.locator("#nav button").count() == len(sections)
    labels = _texts(page.locator("#nav button"))
    for expected in ("Latest", "Rinse FM", "Favourites", "Search"):
        assert expected in labels
    assert _text(page.locator("#title")) == "Latest"
    assert _texts(page.locator("#list .row .row-label")) == [
        "Boot Pack", "Boot Episode",
    ]
    # The player footer stays hidden until a track is chosen (no auto-play).
    assert page.locator("#player").count() == 1
    assert page.locator("#audio").count() == 1
    assert "ITEMS" in _text(page.locator("#status"))
    assert browser_page.page_errors == []


def test_section_navigation_renders_rows(browser_page):
    page = browser_page.page
    page.get_by_role("button", name="Quick Filters", exact=True).click()
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
    assert _text(page.locator("#nav button[aria-current='page']")) == "Quick Filters"
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

def test_pirate_radio_archive_tile_renders_curated_stations(browser_page):
    """1.6.14: the curated Pirate Radio Archives tile is wired to /api/pirate."""
    page = browser_page.page
    page.get_by_role("button", name="Pirate Radio Archives", exact=True).click()
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
