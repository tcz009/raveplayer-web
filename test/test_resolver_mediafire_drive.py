# -*- coding: utf-8 -*-
"""Mediafire/Drive adapters for the resolver service (offline)."""

import pytest

from core import drive as drive_mod
from core import ezgarage
from core.resolver import base
from core.resolver.drive import DriveResolver
from core.resolver.mediafire import MediafireResolver
from core.resolver.service import build_default_service


def test_mediafire_matches_and_resolves(monkeypatch):
    resolver = MediafireResolver(clock=lambda: 1000.0)
    assert resolver.matches("https://www.mediafire.com/file/abc/x.mp3/file")
    assert not resolver.matches("https://example.com/x.mp3")
    monkeypatch.setattr(ezgarage, "resolve_page",
                        lambda page: "https://download1.mediafire.com/x/y.mp3")
    got = resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")
    assert got.url.endswith(".mp3")
    assert got.source == "mediafire"
    assert got.expires_at == 1000.0 + 2 * 60 * 60
    assert "Referer" in got.headers


def test_mediafire_errors_map(monkeypatch):
    resolver = MediafireResolver()
    def boom(page):
        raise ezgarage.UnknownSet("gone", code=404)
    monkeypatch.setattr(ezgarage, "resolve_page", boom)
    with pytest.raises(base.NotFound):
        resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")

    def limited(page):
        raise ezgarage.MediafireRateLimited("slow", code=503, retry_after=30)
    monkeypatch.setattr(ezgarage, "resolve_page", limited)
    with pytest.raises(base.RateLimited):
        resolver.resolve("https://www.mediafire.com/file/abc/x.mp3/file")


def test_drive_matches_and_resolves(monkeypatch):
    resolver = DriveResolver(clock=lambda: 2000.0)
    url = "https://drive.google.com/file/d/ABC123/view"
    assert resolver.matches(url)
    assert not resolver.matches("https://example.com/file/d/ABC/view")
    monkeypatch.setattr(drive_mod, "resolve", lambda value: "https://cdn.example/audio.mp3")
    got = resolver.resolve(url)
    assert got.url == "https://cdn.example/audio.mp3"
    assert got.source == "drive"
    assert got.expires_at == 2000.0 + 2 * 60 * 60


def test_drive_errors_map(monkeypatch):
    resolver = DriveResolver()
    def missing(value):
        raise drive_mod.UnknownDriveFile("no file", code=404)
    monkeypatch.setattr(drive_mod, "resolve", missing)
    with pytest.raises(base.NotFound):
        resolver.resolve("https://drive.google.com/file/d/ABC123/view")


def test_default_service_has_all_adapters():
    service = build_default_service()
    sources = {getattr(a, "source", "") for a in service.adapters}
    assert {"direct", "podomatic", "mediafire", "drive", "mega"} <= sources
