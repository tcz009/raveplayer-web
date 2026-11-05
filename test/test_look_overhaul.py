# -*- coding: utf-8 -*-
"""Look overhaul 1.6.51: 2026 grid tokens, focus furniture and row facts.

These are the parts the browser cannot cheaply assert: the shared design
tokens, the Rave Screen focus-bar language on the rail/rows, and the
standby/error plates.  Live layout is covered by ``test_e2e/test_smoke.py``.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def _block(source, selector):
    start = source.index(selector)
    end = source.index("\n}", start)
    return source[start:end]


def test_print_grid_tokens_are_declared():
    root = _block(_read("static/app.css"), ":root")
    for token in ("--space-1: 4px", "--space-2: 8px", "--space-3: 12px",
                  "--space-6: 24px", "--space-12: 48px", "--row-h: 48px"):
        assert token in root, token
    assert "--focus-ring: var(--acid)" in root
    assert "--shadow-hard-sm: 3px 3px 0 var(--magenta)" in root


def test_focus_ring_is_a_single_token():
    css = _read("static/app.css")
    focus = _block(css, ":focus-visible {")
    assert "outline: 2px solid var(--focus-ring)" in focus
    assert "outline-offset: 2px" in focus
    light = _block(css, 'html[data-theme="light"] {')
    assert "--focus-ring: #07070b" in light, "ink ring on paper"


def test_rail_active_uses_the_rave_screen_focus_bar():
    css = _read("static/app.css")
    dark = _block(css, ".rail button.active {")
    assert "inset 4px 0 0 var(--acid)" in dark
    assert "var(--shadow-hard-sm)" in dark
    light = _block(css, 'html[data-theme="light"] .rail button.active {')
    assert "inset 4px 0 0 var(--acid)" in light
    assert "3px 3px 0 var(--sticker-shadow)" in light


def test_rows_use_the_48px_row_unit_and_a_hard_focus_bar():
    css = _read("static/app.css")
    row = _block(css, ".row {")
    assert "grid-template-columns: var(--row-h) minmax(0, 1fr) auto" in row
    assert "padding: var(--space-2) var(--space-3)" in row
    art = _block(css, ".row-art {")
    assert "width: var(--row-h)" in art
    assert "height: var(--row-h)" in art
    hover = _block(css, ".row:hover {")
    assert "inset 4px 0 0 var(--acid)" in hover


def test_row_fact_is_mono_and_built_with_text_content():
    css = _read("static/app.css")
    fact = _block(css, ".row-fact {")
    assert "font-family: var(--font-mono)" in fact
    assert "font-size: 11px" in fact
    js = _read("static/app.js")
    assert 'fact.className = "row-fact"' in js
    assert "fact.textContent = factText.slice(0, 16)" in js
    # The 16-character budget is the Kodi Label2 budget; no HTML is parsed.
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_empty_and_error_states_are_status_plates():
    css = _read("static/app.css")
    empty = _block(css, ".empty {")
    assert "border-left: 4px solid var(--acid)" in empty
    assert 'content: "STANDBY  "' in css
    state = _block(css, ".state {")
    assert "border-left: 4px solid var(--acid)" in state
    error = _block(css, ".state.is-error {")
    assert "border-left-color: var(--magenta)" in error
    light = _block(css, 'html[data-theme="light"] .empty {')
    assert "border-left-color: var(--magenta-ink)" in light


def test_no_external_font_is_added():
    css = _read("static/app.css")
    assert "@font-face" not in css
    assert "@import url(" not in css
    assert "Impact" in _block(css, ":root")
