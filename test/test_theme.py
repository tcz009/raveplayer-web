# -*- coding: utf-8 -*-
"""1.6.35 — light "rave flyer" theme, appearance switch and WCAG tokens.

The offline checks cover the contract the browser cannot cheaply prove:
- the light palette is a token block and every text/control pair clears AA;
- the early boot script is external, blocking, before the stylesheet and
  CSP-safe (no inline JS);
- the Help dialog exposes real Dark/Light/System radios, 44 px and labelled;
- Node exercises static/theme.js persistence and prefers-color-scheme directly.
The e2e suite covers the live toggle, reload, media changes and screenshots.
"""

import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NODE = shutil.which("node")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def _token_block(css, selector):
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert match, "token block missing: " + selector
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1)))


def _rgb(value):
    value = value.strip()
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    assert match, "not a 6-digit hex token: " + value
    hex_value = match.group(1)
    return tuple(int(hex_value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _luminance(value):
    channels = []
    for channel in _rgb(value):
        channels.append(channel / 12.92 if channel <= 0.04045
                        else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground, background):
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


# --------------------------------------------------------------------------- tokens


def test_light_theme_is_a_token_block_on_the_html_element():
    css = _read("static/app.css")
    light = _token_block(css, 'html[data-theme="light"]')
    assert light["--bg"] == "#f2eee3", "paper page background"
    assert light["--text"] == "#07070b", "ink body type"
    assert light["--paper"] == "#07070b", "--paper is the type token in light mode"
    assert light["--stock"] == "#f2eee3", "sticker stock stays paper"
    assert light["--acid"] == "#e8ff00"
    assert light["--magenta"] == "#ff2d8f"
    assert light["--cyan"] == "#22e6ff"
    assert light["--sticker-shadow"] == "#07070b", "hard offsets are ink in light mode"
    assert light["--sticker-shadow-2"] == "#07070b"
    assert "color-scheme: light" in css
    # The light theme must not introduce a second stylesheet or font.
    assert "Impact" in _token_block(css, ":root")["--font-display"]
    assert "@font-face" not in css


def test_dark_theme_tokens_are_unchanged():
    dark = _token_block(_read("static/app.css"), ":root")
    assert dark["--bg"] == "#07070b"
    assert dark["--paper"] == "#f2eee3"
    assert dark["--text"] == "#f2eee3"
    assert dark["--acid"] == "#e8ff00"
    assert dark["--magenta"] == "#ff2d8f"
    assert dark["--cyan"] == "#22e6ff"


def test_text_and_control_pairs_pass_wcag_aa_in_both_modes():
    css = _read("static/app.css")
    dark = _token_block(css, ":root")
    light = _token_block(css, 'html[data-theme="light"]')
    pairs = [
        ("dark body", dark, "--text", "--bg"),
        ("dark secondary", dark, "--muted", "--panel-3"),
        ("dark metadata", dark, "--faint", "--panel-3"),
        ("dark ink on acid", dark, "--ink", "--acid"),
        ("dark ink on magenta", dark, "--ink", "--magenta"),
        ("dark ink on cyan", dark, "--ink", "--cyan"),
        ("light body", light, "--text", "--bg"),
        ("light secondary", light, "--muted", "--panel-3"),
        ("light metadata", light, "--faint", "--panel-3"),
        ("light ink on paper", light, "--ink", "--stock"),
        ("light ink on acid", light, "--ink", "--acid"),
        ("light ink on magenta", light, "--ink", "--magenta"),
        ("light ink on cyan", light, "--ink", "--cyan"),
        ("light acid text", light, "--acid-ink", "--stock"),
        ("light magenta text", light, "--magenta-ink", "--stock"),
        ("light cyan text", light, "--cyan-ink", "--stock"),
        ("light warning text", light, "--orange-ink", "--panel"),
    ]
    failures = []
    for label, tokens, foreground, background in pairs:
        ratio = _contrast(tokens[foreground], tokens[background])
        if ratio < 4.5:
            failures.append("%s: %s on %s = %.2f:1" % (
                label, foreground, background, ratio))
    assert not failures, "WCAG AA failures:\n" + "\n".join(failures)


def test_light_mode_uses_ink_offsets_and_softens_the_print_texture():
    css = _read("static/app.css")
    light = css[css.index("1.6.35 — LIGHT"):]
    # No magenta/cyan hard sticker offsets survive in the light block.
    assert not re.search(r"box-shadow:[^;]*var\(--magenta\)", light)
    assert not re.search(r"box-shadow:[^;]*var\(--cyan\)", light)
    assert "box-shadow: 4px 4px 0 var(--sticker-shadow)" in light
    # Softer screen: no dark-ink scanline and no heavy 0.22 ink line.
    halftone = re.search(r'html\[data-theme="light"\] \.halftone \{(.*?)\n\}', light, re.S).group(1)
    assert "rgba(7, 7, 11, 0.045)" in halftone
    assert "rgba(7, 7, 11, 0.22)" not in halftone
    # Ready-made focus rule for the light paper.
    assert 'html[data-theme="light"] :focus-visible { outline-color: var(--ink); }' in css
    # Preview text/surfaces stay themed; nothing hard-codes dark paper.
    assert ".surprise-reveal { background: rgba(242, 238, 227, 0.92); }" in light


# --------------------------------------------------------------------------- markup


def test_theme_boot_script_is_external_blocking_and_runs_before_the_css():
    html = _read("templates/index.html")
    theme_at = html.index('src="/static/theme.js')
    css_at = html.index('href="/static/app.css')
    assert theme_at < css_at, "theme.js must set data-theme before app.css"
    tag = html[html.index("<script", theme_at - 80):html.index(">", theme_at) + 1]
    assert "defer" not in tag and "async" not in tag, "the boot script must block"
    # CSP script-src 'self': every script is external, no inline JS is added.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "javascript:" not in html.lower()
    # A no-JS page still paints the original dark theme.
    assert '<html lang="en" data-theme="dark" data-theme-mode="dark">' in html
    assert '<meta name="color-scheme" content="dark light">' in html


def test_help_dialog_has_a_labelled_dark_light_system_switch():
    html = _read("templates/index.html")
    radios = re.findall(
        r'<input\b[^>]*\bname="appearance"[^>]*\bvalue="(dark|light|system)"[^>]*>',
        html)
    assert radios == ["dark", "light", "system"]
    assert '<legend class="help-sub">Appearance</legend>' in html
    for marker in ("Dark", "Light", "System"):
        assert ">%s</span>" % marker in html
    assert "The choice is saved on this device only." in html
    assert 'type="radio"' in html and "sr-only" in html
    # The existing Help dialog owns it; the debug switch stays.
    assert 'id="debug-toggle"' in html
    assert 'id="help"' in html


def test_appearance_switch_is_44px_and_js_owned():
    css = _read("static/app.css")
    block = re.search(r"\.appearance-option \{(.*?)\n\}", css, re.S).group(1)
    assert "min-height: 44px" in block
    assert ".appearance-option:focus-within" in css
    assert ".appearance-option.is-checked" in css
    assert "@media (prefers-reduced-motion: reduce)" in css

    js = _read("static/app.js")
    assert "window.RaveTheme" in js
    assert "function syncAppearance" in js
    assert 'THEME.set(mode)' in js
    assert 'input[name="appearance"]' in js
    assert 'debugEvent("theme"' in js
    assert "innerHTML" not in js

    boot = _read("static/theme.js")
    assert 'var STORAGE_KEY = "raveplayer.theme";' in boot
    assert 'DEFAULT_MODE = "dark"' in boot
    assert "prefers-color-scheme: light" in boot
    assert 'root.setAttribute("data-theme", theme);' in boot
    assert "innerHTML" not in boot


# --------------------------------------------------------------------------- JS


@pytest.mark.skipif(NODE is None, reason="Node is required for the JS unit checks")
def test_theme_js_unit_checks():
    script = os.path.join(HERE, "js", "theme_spec.js")
    result = subprocess.run([NODE, script], cwd=ROOT, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS 7 theme.js unit checks" in result.stdout
