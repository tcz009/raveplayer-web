# -*- coding: utf-8 -*-
"""Web pagination contract (1.6.53): 40/page, numbered links, no gaps."""

import app as app_module
from core import toronto


def _walk(web, url):
    """Follow pager.next and return every item, plus the page metadata."""
    items, pages, page = [], [], 1
    while True:
        separator = "&" if "?" in url else "?"
        payload = web.get("%s%spage=%d" % (url, separator, page)).get_json()
        items.extend(payload["items"])
        pages.append(payload["pager"])
        if not payload["pager"]["next"]:
            return items, pages
        page += 1


def _toronto_rows(count):
    return [{"title": "Toronto mix %03d" % index,
             "url": "https://torontoravemixtapearchive.com/files/mixtape/%d.mp3" % index,
             "date": "1993-09-04", "venue": "Syrous", "duration": "40:00"}
            for index in range(count)]


def test_pager_metadata_uses_the_owner_wording_and_urls(web, archive_search):
    archive_search["state"]["total"] = 400
    payload = web.get("/api/search?q=kool").get_json()
    pager = payload["pager"]
    assert payload["total"] == 400
    assert pager["page"] == 1 and pager["pages"] == 10
    assert pager["per_page"] == 40
    assert pager["prev"] is None
    assert pager["next"] == "/api/search?q=kool&page=2"
    # 1 … N is a bounded window, never one link per page.
    assert pager["links"] == [1, 2, 3, None, 10]
    assert len(pager["links"]) < pager["pages"]


def test_middle_and_last_page_links(web, archive_search):
    archive_search["state"]["total"] = 85
    page3 = web.get("/api/search?q=kool&page=3").get_json()
    assert page3["pager"]["page"] == 3 and page3["pager"]["pages"] == 3
    assert page3["pager"]["next"] is None
    assert page3["pager"]["prev"] == "/api/search?q=kool&page=2"
    assert page3["pager"]["links"] == [1, 2, 3]


def test_out_of_range_page_clamps_to_the_last_page(web, archive_search):
    archive_search["state"]["total"] = 85
    payload = web.get("/api/search?q=kool&page=999").get_json()
    assert payload["pager"]["page"] == 3


def test_toronto_1001_rows_page_without_duplicates_or_gaps(web, monkeypatch):
    monkeypatch.setattr(toronto, "_genre_rows", lambda genre: _toronto_rows(1001))
    first = web.get("/api/toronto?genre=jungle").get_json()
    assert len(first["items"]) == 40
    assert first["pager"]["pages"] == 26
    assert first["pager"]["total"] == 1001
    items, pages = _walk(web, "/api/toronto?genre=jungle")
    assert len(items) == 1001
    labels = [item["label"] for item in items]
    assert labels[0] == "Toronto mix 000"
    assert labels[-1] == "Toronto mix 1000"
    assert len(set(labels)) == 1001
    assert [page["page"] for page in pages] == list(range(1, 27))
    assert pages[-1]["pages"] == 26 and pages[-1]["next"] is None
    last = web.get("/api/toronto?genre=jungle&page=26").get_json()
    assert len(last["items"]) == 1


def test_moxser_1001_mixes_page(web, monkeypatch):
    mixes = [{"title": "Mox %04d" % index, "slug": "mox-%04d" % index,
              "date": "2026-01-01", "flyer": ""} for index in range(1001)]
    monkeypatch.setattr(app_module.moxser, "mixes", lambda: mixes)
    first = web.get("/api/moxser").get_json()
    assert first["pager"]["pages"] == 26
    assert len(first["items"]) == 40
    items, _pages = _walk(web, "/api/moxser")
    assert len(items) == 1001
    assert len({item["label"] for item in items}) == 1001


def test_jungletapes_310_shows_page(web, monkeypatch):
    shows = [{"key": "show-%03d" % index, "name": "Show %03d" % index}
             for index in range(310)]
    monkeypatch.setattr(app_module.legacy, "jungle_shows", lambda: shows)
    first = web.get("/api/legacy/jungletapes").get_json()
    assert first["pager"]["pages"] == 8
    assert len(first["items"]) == 40
    items, _pages = _walk(web, "/api/legacy/jungletapes")
    assert len(items) == 310
    assert items[-1]["label"] == "Show 309"


def test_rave_nights_page(web, monkeypatch):
    entries = [{"name": "Rave %03d" % index, "slug": "rave-%03d" % index,
                "archive_count": index, "mixcloud_count": 0}
               for index in range(55)]
    monkeypatch.setattr(app_module.raves, "registry", lambda: entries)
    first = web.get("/api/raves").get_json()
    assert first["pager"]["pages"] == 2
    assert len(first["items"]) == 40
    items, _pages = _walk(web, "/api/raves")
    assert len(items) == 55


def test_live_radio_pages_at_40(web, monkeypatch):
    stations = [{"name": "Station %03d" % index,
                 "url_resolved": "https://stream-%03d.example/live" % index}
                for index in range(120)]
    monkeypatch.setattr(app_module.radio, "stations",
                        lambda tag, limit=500: stations)
    first = web.get("/api/live/jungle").get_json()
    assert first["pager"]["pages"] == 3
    assert len(first["items"]) == 40
    items, _pages = _walk(web, "/api/live/jungle")
    assert len(items) == 120
    assert len({item["label"] for item in items}) == 120


def test_downloads_page(web, monkeypatch):
    entries = [{"id": "download-%03d" % index, "title": "Download %03d" % index,
                "size": 1024, "created": 0, "source": "archive"}
               for index in range(120)]
    monkeypatch.setattr(app_module.downloads, "list_entries",
                        lambda base, identity: entries)
    monkeypatch.setattr(app_module.downloads, "all_entries",
                        lambda base: entries)
    first = web.get("/api/downloads").get_json()
    assert first["pager"]["pages"] == 3
    assert first["storage"]["items"] == 120
    items, _pages = _walk(web, "/api/downloads")
    assert len(items) == 120


def test_rtp_server_pages_get_a_cursor_pager(web, monkeypatch):
    posts = [{"id": str(index), "name": "Pack %d" % index} for index in range(10)]
    monkeypatch.setattr(app_module.legacy, "all_packs",
                        lambda page=1: (posts, "next" if page == 1 else ""))
    first = web.get("/api/legacy/ravetapepacks/all").get_json()
    pager = first["pager"]
    assert pager["page"] == 1 and pager["pages"] is None
    assert pager["next"] == "/api/legacy/ravetapepacks/all?page=2"
    assert pager["prev"] is None
    second = web.get("/api/legacy/ravetapepacks/all?page=2").get_json()
    assert second["pager"]["pages"] is None
    assert second["pager"]["prev"].endswith("page=1")
    assert second["pager"]["next"] is None


def test_rtp_index_pages_without_the_optional_slice(web, monkeypatch):
    cats = {"events": [], "djs": [{"id": str(index), "name": "DJ %03d" % index}
                                  for index in range(209)],
            "mcs": [], "years": []}
    monkeypatch.setattr(app_module.legacy, "categories", lambda: cats)
    first = web.get("/api/legacy/ravetapepacks/index/djs").get_json()
    assert first["pager"]["pages"] == 6
    assert len(first["items"]) == 40
    # Explicit ?limit= keeps the old opt-in slice contract.
    legacy = web.get("/api/legacy/ravetapepacks/index/djs?limit=5&offset=2").get_json()
    assert len(legacy["items"]) == 5
    assert "pager" not in legacy


def test_pager_markup_and_script_markers_are_present():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "templates", "index.html"), encoding="utf-8") as handle:
        html = handle.read()
    with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as handle:
        js = handle.read()
    for marker in ('id="pager"', 'id="pager-top"', 'id="page-meta"',
                   'aria-label="Pagination"', 'aria-live="polite"'):
        assert marker in html, marker
    assert 'id="more"' not in html and "more-btn" not in js
    for marker in ("function renderPager", "aria-current", "ArrowLeft",
                   "ArrowRight", "focusList", "normalizePager"):
        assert marker in js, marker
    assert "innerHTML" not in js


def test_dnb_shows_page(web, monkeypatch):
    shows = [{"id": index, "title": "Show %03d" % index, "date": "2023-11-22"}
             for index in range(90)]
    monkeypatch.setattr(app_module.dnbradio, "mixes", lambda: shows)
    first = web.get("/api/dnb").get_json()
    assert first["pager"]["pages"] == 3
    assert len(first["items"]) == 40
    items, _pages = _walk(web, "/api/dnb")
    assert len(items) == 90
    assert len({item["label"] for item in items}) == 90
