# -*- coding: utf-8 -*-
"""Scoped ``RAVEWEB_RESOLVER_TOKEN`` for the Kodi media player.

The service token is not a general ``/api/*`` credential: it may read only the
exact source lists the Kodi client needs (plus their paginated GET children).
Favourites, downloads, admin, streaming/download routes, other APIs and every
non-GET method stay session-only.
"""

import pytest

import app as app_module

TOKEN = "kodi-service-token"


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setattr(app_module, "RESOLVER_SERVICE_TOKEN", TOKEN)


def _headers(token=TOKEN):
    return {"Authorization": "Bearer %s" % token}


def _patch_read_sources(monkeypatch):
    """Keep every allowlisted route offline and deterministic."""
    monkeypatch.setattr(app_module.bensons, "entries", lambda: [])
    monkeypatch.setattr(app_module.ukraves, "page_entries",
                        lambda page: ([], False))
    monkeypatch.setattr(app_module.ukraves, "describe_many", lambda rows: [])
    monkeypatch.setattr(app_module.latest, "merge", lambda limit=60: [])
    monkeypatch.setattr(app_module.feeds, "feed",
                        lambda feed_id: {"id": "f", "name": "Feed"}
                        if feed_id == "f" else None)
    monkeypatch.setattr(app_module.feeds, "episodes",
                        lambda feed_id, limit=60: [])
    monkeypatch.setattr(app_module.radio, "stations",
                        lambda tag, limit=100: [])


@pytest.mark.parametrize("path,title", [
    ("/api/20bensons", "20bensons.com"),
    ("/api/ukraves", "UK Raves Downloads"),
    ("/api/ezgarage", "EZ Garage Sets"),
    ("/api/live", "Live Radio"),
    ("/api/live/jungle", "Jungle"),
    ("/api/podcast/f", "Feed"),
    ("/api/latest", "Latest"),
])
def test_token_reads_allowlisted_source_apis(anon, monkeypatch, path, title):
    _patch_read_sources(monkeypatch)
    response = anon.get(path, headers=_headers())
    assert response.status_code == 200, (path, response.data)
    assert response.is_json
    assert response.get_json()["title"] == title


def test_x_resolve_token_header_still_works(anon, monkeypatch):
    _patch_read_sources(monkeypatch)
    response = anon.get("/api/latest", headers={"X-Resolve-Token": TOKEN})
    assert response.status_code == 200


@pytest.mark.parametrize("path", [
    "/api/favourites",
    "/api/downloads",
    "/api/admin/whoami",
    "/api/admin/stats",
    "/api/menu",
    "/api/surprise",
    "/api/podcasts",  # must not match the "/api/podcast/" subtree
    "/api/ezgarage/72aswcrx4hlw2sh/file",
    "/api/ezgarage/72aswcrx4hlw2sh/stream",
    "/api/ukraves/7524/file",
    "/api/ukraves/7524/stream",
    "/api/mega/stream",
])
def test_token_is_rejected_outside_the_allowlist(anon, path):
    response = anon.get(path, headers=_headers())
    assert response.status_code == 401, (path, response.data)


@pytest.mark.parametrize("path", [
    "/api/20bensons",
    "/api/ukraves",
    "/api/ezgarage",
    "/api/live",
    "/api/live/jungle",
    "/api/podcast/f",
    "/api/latest",
])
def test_token_does_not_authorise_post(anon, monkeypatch, path):
    _patch_read_sources(monkeypatch)
    response = anon.post(path, headers=_headers())
    assert response.status_code == 401, (path, response.data)


@pytest.mark.parametrize("wrong", [
    "",
    "wrong-token",
    "kodi-service-toke",
    "KODI-SERVICE-TOKEN",
    "kodi-service-tokenx",
])
def test_wrong_token_is_rejected(anon, monkeypatch, wrong):
    _patch_read_sources(monkeypatch)
    response = anon.get("/api/latest", headers=_headers(wrong))
    assert response.status_code == 401


def test_allowlisted_route_still_requires_a_credential(anon, monkeypatch):
    _patch_read_sources(monkeypatch)
    assert anon.get("/api/latest").status_code == 401
