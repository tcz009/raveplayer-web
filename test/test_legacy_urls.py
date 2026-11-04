# -*- coding: utf-8 -*-
"""RaveTapePacks URL building + stream-proxy resolution (offline).

The live site's hotlink guard 403s a bare GET and serves audio/mpeg (206 with a
Range) once the Referer/User-Agent are present.  A local HTTP server reproduces
that contract, so a regression in URL building is caught without the network.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import legacy


class _Handler(BaseHTTPRequestHandler):
    server_version = "RaveFixture/1.0"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="audio/mpeg", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        port = self.server.server_address[1]
        root = "http://127.0.0.1:%d/" % port
        if self.path == "/":
            page = (
                '<audio src="/music/Pack One/Tape 1 (Side A) - Swan-E.mp3"></audio>'
                '<audio src="http://127.0.0.1:%d/music/PackTwo/'
                'DJ_SS_&amp;_Twisted_Individual.mp3"></audio>'
                % port
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/retry/music/PackThree/Needs_Directory.mp3":
            # Only the pack directory Referer is accepted: exercises the
            # proxy's one 403 retry without weakening the normal check.
            expected = "http://127.0.0.1:%d/retry/music/PackThree/" % port
            if self.headers.get("Referer") != expected:
                self._send(403, b"Access forbidden!", "text/html; charset=utf-8")
                return
        elif self.path.startswith("/music/"):
            if self.headers.get("Referer") != root or \
                    self.headers.get("User-Agent") != "Mozilla/5.0":
                self._send(403, b"Access forbidden!", "text/html; charset=utf-8")
                return
        else:
            self._send(404, b"missing", "text/plain")
            return
        body = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 32
        if self.headers.get("Range") == "bytes=0-":
            self._send(206, body, extra={
                "Content-Range": "bytes 0-%d/%d" % (len(body) - 1, len(body))})
        else:
            self._send(200, body)


@pytest.fixture(scope="module")
def fixture_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, "http://127.0.0.1:%d/" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _fixture(name):
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "fixtures", name), encoding="utf-8") as handle:
        return handle.read()


def test_packs_normalises_relative_http_entity_and_json_shapes(monkeypatch):
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: _fixture("ravetapepacks-broken.html"))
    packs = legacy.packs()
    urls = set(track["url"] for pack in packs for track in pack["tracks"])
    assert "https://www.ravetapepacks.com/music/Pack%20One/Tape%201%20%28Side%20A%29%20-%20Swan-E.mp3" in urls
    assert "https://www.ravetapepacks.com/music/PackTwo/DJ_SS_%26_Twisted_Individual.mp3" in urls
    # A literal &amp; filename is &amp;amp; in the page: decode exactly once.
    assert "https://www.ravetapepacks.com/music/PackThree/Literal_%26amp%3B_Name.mp3" in urls
    assert "https://www.ravetapepacks.com/music/PackFour/Protocol.mp3" in urls
    assert "https://www.ravetapepacks.com/music/PackFive/Relative.mp3" in urls
    assert "https://www.ravetapepacks.com/music/PackSix/JS_Escaped_%26_Raw.mp3" in urls
    assert len(urls) == 6
    assert all(url.startswith("https://www.ravetapepacks.com/") for url in urls)


def test_packs_rejects_foreign_hosts_bad_schemes_and_traversal(monkeypatch):
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: _fixture("ravetapepacks-broken.html"))
    urls = [track["url"] for pack in legacy.packs() for track in pack["tracks"]]
    assert not any("evil.example" in url for url in urls)
    assert not any(url.startswith("file:") for url in urls)
    assert not any("passwd" in url for url in urls)
    assert not any("/other/" in url for url in urls)


def test_playable_referer_matches_the_url_origin():
    local = legacy.playable("http://127.0.0.1:8765/music/Pack/Tape.mp3")
    assert "|Referer=http://127.0.0.1:8765/&User-Agent=Mozilla/5.0" in local
    site = legacy.playable("https://www.ravetapepacks.com/music/Pack/Tape.mp3")
    assert site.endswith(legacy.REFERER_SUFFIX)


def test_built_urls_resolve_through_api_stream(web, monkeypatch, fixture_site):
    server, base = fixture_site
    import app as app_module

    monkeypatch.setattr(legacy, "BASE", base)
    monkeypatch.setattr(app_module, "ALLOW_PRIVATE", True)
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: (
        '<audio src="/music/Pack One/Tape 1 (Side A) - Swan-E.mp3"></audio>'
        '<audio src="http://127.0.0.1:%d/music/PackTwo/'
        'DJ_SS_&amp;_Twisted_Individual.mp3"></audio>'
        % server.server_address[1]
    ))
    tracks = [track for pack in legacy.packs() for track in pack["tracks"]]
    assert len(tracks) == 2
    for track in tracks:
        response = web.get("/api/stream", query_string={"u": legacy.playable(track["url"])},
                           headers={"Range": "bytes=0-"})
        assert response.status_code == 206, response.get_data(as_text=True)
        assert response.headers["Content-Type"].startswith("audio/")
        assert response.headers["Content-Range"].startswith("bytes 0-")
        assert response.data.startswith(b"ID3")


def test_proxy_retries_a_403_with_the_pack_directory_referer(web, monkeypatch, fixture_site):
    server, base = fixture_site
    import app as app_module

    monkeypatch.setattr(app_module, "ALLOW_PRIVATE", True)
    monkeypatch.setattr(app_module, "_REFERER_RETRY_HOSTS",
                        ("ravetapepacks.com", "127.0.0.1"))
    target = base + "retry/music/PackThree/Needs_Directory.mp3"
    # Root Referer is refused; the proxy's one retry uses the file directory.
    response = web.get("/api/stream", query_string={"u": legacy.playable(target)},
                       headers={"Range": "bytes=0-"})
    assert response.status_code == 206, response.get_data(as_text=True)
    assert response.data.startswith(b"ID3")


def test_proxy_does_not_retry_a_403_for_other_hosts(web, monkeypatch, fixture_site):
    server, base = fixture_site
    import app as app_module

    monkeypatch.setattr(app_module, "ALLOW_PRIVATE", True)
    target = base + "retry/music/PackThree/Needs_Directory.mp3"
    # A directory Referer is the only one that works, but a non-RaveTapePacks
    # host must not get the retry: the first 403 is reported.
    response = web.get("/api/stream", query_string={"u": target + "|Referer=%s" % base},
                       headers={"Range": "bytes=0-"})
    assert response.status_code == 403


def test_jungle_tracks_validate_upgrade_and_dedupe(monkeypatch):
    page = (
        '<a href="file:///tmp/x.mp3">x</a>'
        '<a href="javascript:alert(1)//x.mp3">js</a>'
        '<a href="http://www.jungletapes.com/a.mp3">one</a>'
        '<a href="https://www.jungletapes.com/a.mp3">one again</a>'
        '<a href="https://www.jungletapes.com/a.mp3?x=1&amp;y=2">query</a>'
    )
    monkeypatch.setattr(legacy.http, "fetch", lambda *a, **k: page)
    urls = [track["url"] for track in legacy.jungle_tracks("show")]
    assert urls == [
        "https://www.jungletapes.com/a.mp3",
        "https://www.jungletapes.com/a.mp3?x=1&y=2",
    ]


def test_hardscore_media_urls_are_absolute_and_deduped():
    from core import sources

    text = (
        'file:///tmp/x.mp3 javascript:alert(1)//y.mp3 '
        'https://www.hardscore.com/a.mp3 '
        'https://www.hardscore.com/a.mp3 '
        'https://www.hardscore.com/b.mp3?x=1&amp;y=2'
    )
    assert sources.media_urls(text) == [
        "https://www.hardscore.com/a.mp3",
        "https://www.hardscore.com/b.mp3?x=1&y=2",
    ]


def test_category_pack_link_resolves_from_its_category(web, monkeypatch):
    import app as app_module

    pack = {"key": "AmazonVol10", "name": "Amazon Vol 10",
            "tracks": [{"name": "Fabio",
                        "url": "https://www.ravetapepacks.com/music/"
                               "AmazonVol10/Tape_1_-_Fabio.mp3"}]}
    monkeypatch.setattr(app_module.legacy, "packs", lambda: [])
    monkeypatch.setattr(app_module.legacy, "category_packs",
                        lambda kind, cat_id: [pack] if (kind, cat_id) == ("djs", "106") else [])
    payload = web.get("/api/legacy/ravetapepacks/index/djs/106").get_json()
    open_url = payload["items"][0]["open"]
    assert "?cat=106&kind=djs" in open_url
    detail = web.get(open_url)
    assert detail.status_code == 200
    assert detail.get_json()["items"][0]["play"].endswith(app_module.legacy.REFERER_SUFFIX)


def test_hardscore_enclosure_is_not_double_decoded():
    from core import sources

    xml = (
        '<rss version="2.0"><channel><item>'
        '<title>Set</title><link>https://www.hardscore.com/?p=1</link>'
        '<enclosure url="https://hardscore.com/mixes/A_&amp;amp;_B.mp3" '
        'type="audio/mpeg"/>'
        '<description><![CDATA[<a href="https://hardscore.com/mixes/'
        'C_&amp;amp;_D.mp3">x</a>]]></description>'
        '</item></channel></rss>'
    )
    episode = sources.parse_rss(xml)[0]
    assert "https://hardscore.com/mixes/A_&amp;_B.mp3" in episode["media"]
    assert "https://hardscore.com/mixes/C_&amp;_D.mp3" in episode["media"]
    assert "https://hardscore.com/mixes/A_&_B.mp3" not in episode["media"]
