# -*- coding: utf-8 -*-
"""1.6.45 — inline embed dock and the single-audio-source policy.

The provider widget API checks run under Node against the real
``static/embed.js``; the markup/CSS/JS checks pin the parts a browser suite
would otherwise have to discover one viewport at a time.
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


def _section(html, opening):
    start = html.index(opening)
    end = html.index("</section>", start)
    return html[start:end]


def _block(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start + len(start_marker))
    return source[start:end]


# --------------------------------------------------------------------------- markup


def test_embed_dock_is_a_region_not_a_dialog():
    html = _read("templates/index.html")
    dock = _section(html, '<section id="embed-panel" class="embed-dock"')
    assert 'role="region"' in dock
    assert 'aria-labelledby="embed-title"' in dock
    assert 'tabindex="-1"' in dock
    # The old modal take-over is gone: no dialog role, no aria-modal, no scrim.
    assert 'role="dialog"' not in dock
    assert "aria-modal" not in dock
    assert 'class="embed-card"' not in dock
    # The heading is the focus target and the close control is labelled.
    assert '<h2 id="embed-title" class="embed-title" tabindex="-1">' in dock
    assert 'id="embed-close"' in dock
    assert 'aria-label="Close embedded player"' in dock
    # Live status for widget play/pause where the provider reports it.
    assert 'id="embed-status"' in dock
    assert 'id="embed-note"' in dock
    assert 'aria-live="polite"' in dock


def test_embed_bridge_loads_before_the_app():
    html = _read("templates/index.html")
    debug_at = html.index("/static/debug.js")
    embed_at = html.index("/static/embed.js")
    app_at = html.index("/static/app.js")
    assert debug_at < embed_at < app_at
    assert "defer" in html[embed_at:embed_at + 90]


# --------------------------------------------------------------------------- styling


def test_embed_dock_is_a_bottom_dock_without_scroll_lock_or_backdrop():
    css = _read("static/app.css")
    assert ".embed-dock {" in css
    dock = css[css.index(".embed-dock {"):css.index(".embed-dock-inner")]
    assert "position: fixed" in dock
    assert "bottom: 0" in dock
    assert "inset: 0" not in dock
    # No full-screen scrim, no modal card, no page scroll lock.
    assert ".embed-card" not in css
    assert "body.has-embed { overflow: hidden" not in css
    assert "body.has-embed .stage" in css
    assert "--embed-dock-h" in css
    assert "body.has-embed .rail" in css
    # A height cap keeps the widget a dock rather than a third of the screen.
    frame = css[css.index(".embed-frame-wrap {"):css.index(".embed-frame {")]
    assert "height: clamp(" in frame
    assert "padding-top: 56.25%" not in frame


def test_embed_dock_has_a_390px_layout():
    css = _read("static/app.css")
    mobile = css[css.index("@media (max-width: 620px)", css.index(".embed-dock {")):]
    block = mobile[:mobile.index("\n}\n") + 3]
    assert ".embed-dock" in block
    assert ".embed-frame-wrap" in block
    assert ".embed-close" in block
    assert "height: clamp(" in block


def test_embed_dock_keeps_focus_visible_and_reduced_motion():
    css = _read("static/app.css")
    assert ".embed-title:focus-visible" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation-duration: 0.001ms !important" in reduced
    # The dock's only motion is the shared transmission line, disabled above.
    assert "animation: transmit" in css


# --------------------------------------------------------------------------- single audio


def test_open_embed_stops_the_main_player_before_loading_the_widget():
    js = _read("static/app.js")
    open_embed = _block(js, "  function openEmbed(item, trigger) {", "\n  function markEmbedLoaded")
    close_at = open_embed.index("closePlayer()")
    src_at = open_embed.index('frame.setAttribute("src", embedState.url)')
    assert close_at < src_at, "the main audio must be cleared before the iframe src is set"
    assert "freshEmbedFrame()" in open_embed
    assert "EMBED_API.attach" in open_embed


def test_every_main_playback_path_closes_the_embed():
    js = _read("static/app.js")
    for start, end in (
        ("  async function resolveAndPlay(item, index) {", "\n  function playItem"),
        ("  function playItem(item, url, index) {", "\n  function togglePlay"),
        ("  function togglePlay() {", "\n  function retryPlayback"),
        ("  function retryPlayback() {", "\n  function seekBy"),
        ("  async function navigate(path, options = {}) {", "\n  async function loadMore"),
    ):
        block = _block(js, start, end)
        assert "closeEmbed(" in block, start
    play_handler = _block(js, '  audio.addEventListener("play", () => {', "\n  audio.addEventListener(\"pause\"")
    assert "if (isEmbedOpen()) closeEmbed();" in play_handler


def test_close_embed_discards_the_iframe_and_restores_focus():
    js = _read("static/app.js")
    close_embed = _block(js, "  function closeEmbed(options = {}) {", "\n  function favKey")
    assert "detachEmbedBridge()" in close_embed
    # Replacing the element discards the cross-origin browsing context (the
    # only way to be sure its audio stopped) and leaves a blank #embed-frame.
    assert "freshEmbedFrame()" in close_embed
    assert "panel.hidden = true" in close_embed
    assert "restoreFocus === true" in close_embed
    assert "trigger.focus" in close_embed
    assert "document.body.classList.remove(\"has-embed\")" in close_embed


def test_escape_and_back_close_the_dock():
    js = _read("static/app.js")
    keydown = _block(js, '  document.addEventListener("keydown", (event) => {', "\n  async function boot")
    assert 'event.key === "Escape" && isEmbedOpen()' in keydown
    assert "closeEmbed({ restoreFocus: true })" in keydown
    back = _block(js, '  $("back").addEventListener("click", () => {', "\n  });")
    assert "isEmbedOpen()" in back
    assert "closeEmbed({ restoreFocus: true })" in back
    # Close button restores focus; the old backdrop handler is gone.
    assert '$("embed-close").addEventListener("click", () => closeEmbed({ restoreFocus: true }))' in js
    assert 'if (event.target === $("embed-panel")) closeEmbed();' not in js


# --------------------------------------------------------------------------- bridge


@pytest.mark.skipif(NODE is None, reason="Node is required for the JS unit checks")
def test_embed_bridge_unit_checks():
    script = os.path.join(HERE, "js", "embed_spec.js")
    result = subprocess.run([NODE, script], cwd=ROOT, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS 13 embed.js unit checks" in result.stdout


def test_embed_bridge_only_accepts_provider_sources_and_origins():
    js = _read("static/embed.js")
    assert "event.source !== frame.contentWindow" in js
    assert "originAllowed(key, event.origin)" in js
    assert "postMessage(payload, origin)" in js
    assert 'postMessage(payload, "*")' not in js, "never target *"
    assert "https://w.soundcloud.com" in js
    assert "https://www.mixcloud.com" in js
    assert "https://player-widget.mixcloud.com" in js
