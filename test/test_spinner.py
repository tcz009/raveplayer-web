# -*- coding: utf-8 -*-
"""1.6.17 spinner pass: sprite assets/CSS, loading-pill state, NDJSON download
progress and cancel state, and the Surprise reveal wiring.

The animation itself is CSS; these tests lock the frames/sheet dimensions, the
keyframes/steps cadence, the reduced-motion override and the JS state hooks, and
exercise the server's progress/cancel state machine with a fake upstream.
"""

import json
import os
import struct
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def _png_size(path):
    with open(path, "rb") as handle:
        head = handle.read(33)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


# --------------------------------------------------------------------------- art


def test_kodi_spinner_frames_are_copied_at_both_sizes():
    frame_dir = os.path.join(ROOT, "static", "art", "spinner")
    for size in (128, 256):
        for index in range(10):
            path = os.path.join(frame_dir, "head-%d-%02d.png" % (size, index))
            assert os.path.isfile(path), path
            assert _png_size(path) == (size, size), path


def test_sprite_sheets_are_ten_tiled_frames():
    frame_dir = os.path.join(ROOT, "static", "art", "spinner")
    assert _png_size(os.path.join(frame_dir, "sprite-128.png")) == (1280, 128)
    assert _png_size(os.path.join(frame_dir, "sprite-256.png")) == (2560, 256)


def test_css_uses_steps_animation_and_reduced_motion():
    css = _read("static/app.css")
    assert "sprite-128.png" in css and "sprite-256.png" in css
    assert "steps(10)" in css
    assert "@keyframes head-spin" in css
    assert "calc(var(--head) * 10)" in css
    # Every animated spinner surface is disabled under reduced motion.
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".spinner-head" in reduced and "animation: none" in reduced
    assert ".dl-bar.indeterminate .dl-bar-fill" in reduced
    assert ".surprise-reveal.landed .surprise-pick" in reduced
    # Panel/pill/reveal are themed, not stock widgets.
    for token in (".download-panel", ".loading-pill", ".surprise-reveal",
                  ".dl-bar-fill", ".surprise-pick"):
        assert token in css, token


def test_markup_has_the_three_spinner_surfaces():
    html = _read("templates/index.html")
    for element_id in ("loading-pill", "download-panel", "dl-title", "dl-bar",
                       "dl-bar-fill", "dl-percent", "dl-size", "dl-remaining",
                       "dl-cancel", "surprise-reveal", "surprise-pick"):
        assert 'id="%s"' % element_id in html, element_id
    assert "aria-live" in html[html.index('id="loading-pill"') - 80:html.index('id="loading-pill"') + 200]


def test_client_state_hooks_and_no_innerhtml():
    js = _read("static/app.js")
    assert "beginLoad" in js and "endLoad" in js
    assert "setTimeout(showLoadingPill, 150)" in js
    assert "application/x-ndjson" in js
    assert "cancelDownload" in js
    assert "SURPRISE_MIN_MS" in js and "SURPRISE_LAND_MS" in js
    assert "prefersReducedMotion" in js
    assert "formatRemaining" in js and "formatBytes" in js
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js


# --------------------------------------------------------------------------- progress state


class _FakeResponse(object):
    status = 200

    def __init__(self, chunks, delay=0.0):
        self._chunks = list(chunks)
        self._delay = delay

    def getheader(self, name):
        lowered = (name or "").lower()
        if lowered == "content-type":
            return "audio/mpeg"
        if lowered == "content-length":
            return str(sum(len(chunk) for chunk in self._chunks))
        return None

    def read(self, size=-1):
        if self._delay:
            time.sleep(self._delay)
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if size and len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            chunk = chunk[:size]
        return chunk

    def close(self):
        pass


class _FakeConnection(object):
    def close(self):
        pass


def _patch_stream(monkeypatch, response, url="https://hardscore.com/mixes/x.mp3"):
    import app as app_module

    monkeypatch.setattr(app_module, "_open_stream",
                        lambda target, headers, deadline: (_FakeConnection(), response, url))


def test_download_chunks_emit_started_progress_and_done(tmp_path, monkeypatch):
    import app as app_module

    response = _FakeResponse([b"a" * 2048, b"b" * 2048, b"c" * 2048], delay=0.11)
    _patch_stream(monkeypatch, response)
    events = list(app_module._download_chunks(
        "https://hardscore.com/mixes/x.mp3", str(tmp_path / "out.mp3"),
        1024 * 1024, 30))
    assert events[0] == {"started": True, "total": 6144}
    progress = [event for event in events if "received" in event]
    assert progress, events
    assert progress[-1]["received"] == 6144
    assert events[-1] == {"done": True, "size": 6144}
    assert (tmp_path / "out.mp3").read_bytes() == b"a" * 2048 + b"b" * 2048 + b"c" * 2048


def test_ndjson_download_stream_reports_progress_and_saves(web, tmp_path, monkeypatch):
    import app as app_module

    _patch_stream(monkeypatch, _FakeResponse([b"x" * 2048] * 3, delay=0.11))
    response = web.post(
        "/api/downloads",
        data=json.dumps({"url": "https://hardscore.com/mixes/test.mp3",
                         "title": "Progress Test", "source": "hardscore",
                         "client_id": "abc12345"}),
        content_type="application/json",
        headers={"Accept": "application/x-ndjson"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.headers["Content-Type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]
    states = [line["state"] for line in lines]
    assert states[0] == "started"
    assert "progress" in states
    assert states[-1] == "saved"
    assert lines[0]["total"] == 6144
    assert lines[-1]["item"]["download_key"]
    assert web.get("/api/downloads").get_json()["items"][0]["label"] == "Progress Test"


def test_download_can_be_cancelled_mid_stream(web, tmp_path, monkeypatch):
    import app as app_module

    _patch_stream(monkeypatch, _FakeResponse([b"x" * 2048] * 3, delay=0.11))
    with app_module._DOWNLOAD_CANCEL_LOCK:
        app_module._DOWNLOAD_CANCELS.add("cancelme1")
    try:
        response = web.post(
            "/api/downloads",
            data=json.dumps({"url": "https://hardscore.com/mixes/cancel.mp3",
                             "title": "Cancel Test", "source": "hardscore",
                             "client_id": "cancelme1"}),
            content_type="application/json",
            headers={"Accept": "application/x-ndjson"},
        )
        lines = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]
        assert lines[-1] == {"state": "cancelled"}
        assert web.get("/api/downloads").get_json()["items"] == []
        download_files = [name for name in os.listdir(tmp_path / "downloads")
                          if name.endswith(".mp3")] if (tmp_path / "downloads").exists() else []
        assert download_files == []
    finally:
        with app_module._DOWNLOAD_CANCEL_LOCK:
            app_module._DOWNLOAD_CANCELS.discard("cancelme1")


def test_cancel_endpoint_validates_and_marks(web):
    import app as app_module

    assert web.post("/api/downloads/cancel/abc12345").get_json() == {"cancelled": True}
    with app_module._DOWNLOAD_CANCEL_LOCK:
        assert "abc12345" in app_module._DOWNLOAD_CANCELS
        app_module._DOWNLOAD_CANCELS.discard("abc12345")
    assert web.post("/api/downloads/cancel/!!").status_code == 400
