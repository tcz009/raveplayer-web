# -*- coding: utf-8 -*-
"""Offline tests for Archive audio-file selection, de-duplication and bounds.

These mirror the Kodi add-on's tests because core/archive.py is a shared
provider: the folder + stem rule keeps Tape 1/01.mp3 and Tape 2/01.mp3 while
dropping the OGG twin of each. The web layer exposes the parsed duration as
``length`` (the add-on calls it ``duration``).
"""

from core import archive


def _files(monkeypatch, files):
    monkeypatch.setattr(
        archive,
        "item",
        lambda identifier: {"metadata": {"title": "Pack"}, "files": files},
    )
    return archive.audio_files("pack")


def test_derivative_ogg_is_not_a_second_track(monkeypatch):
    tracks = _files(monkeypatch, [
        {"name": "Show A.mp3", "source": "original", "format": "VBR MP3", "length": "100.5"},
        {"name": "Show A.ogg", "source": "derivative", "format": "Ogg Vorbis", "length": "100.4"},
        {"name": "Show B.mp3", "source": "original", "format": "VBR MP3", "length": "200"},
    ])
    assert [track["title"] for track in tracks] == ["Show A", "Show B"]
    assert all(track["url"].endswith(".mp3") for track in tracks)


def test_original_wins_over_derivative_with_the_same_stem(monkeypatch):
    tracks = _files(monkeypatch, [
        {"name": "Set.mp3", "source": "derivative", "format": "VBR MP3"},
        {"name": "Set.flac", "source": "original", "format": "FLAC"},
    ])
    assert len(tracks) == 1
    assert tracks[0]["url"].endswith(".flac")


def test_same_stem_in_different_folders_is_kept(monkeypatch):
    tracks = _files(monkeypatch, [
        {"name": "Tape 1/01.mp3", "source": "original", "format": "VBR MP3"},
        {"name": "Tape 2/01.mp3", "source": "original", "format": "VBR MP3"},
    ])
    assert len(tracks) == 2
    assert "Tape%201/01.mp3" in tracks[0]["url"]
    assert "Tape%202/01.mp3" in tracks[1]["url"]


def test_tracks_are_ranked_then_named(monkeypatch):
    tracks = _files(monkeypatch, [
        {"name": "b.mp3", "source": "derivative", "format": "VBR MP3"},
        {"name": "z.flac", "source": "original", "format": "FLAC"},
        {"name": "a.wav", "source": "original", "format": "WAVE"},
    ])
    assert [track["name"] for track in tracks] == ["a.wav", "z.flac", "b.mp3"]


def test_length_is_returned_as_parsed_seconds(monkeypatch):
    tracks = _files(monkeypatch, [
        {"name": "A.mp3", "source": "original", "format": "VBR MP3", "length": "61.9"},
        {"name": "B.mp3", "source": "original", "format": "VBR MP3", "length": "1:02:03"},
    ])
    assert tracks[0]["length"] == 61
    assert tracks[1]["length"] == 3723


def test_duration_parser_handles_archive_shapes():
    assert archive._duration("1872.46") == 1872
    assert archive._duration(65) == 65
    assert archive._duration("1:02:03") == 3723
    assert archive._duration("00:02:30") == 150
    assert archive._duration("") is None
    assert archive._duration(None) is None
    assert archive._duration("0") is None
    assert archive._duration("soon") is None
    # Third-party metadata: non-finite values must not raise OverflowError.
    assert archive._duration("inf") is None
    assert archive._duration("1e400") is None
    assert archive._duration("nan") is None
    assert archive._duration(float("inf")) is None
    assert archive._duration(float("nan")) is None


def test_search_rows_and_page_are_clamped(monkeypatch):
    seen = {}

    def fake_fetch(url, **kwargs):
        seen["url"] = url
        return {"response": {"docs": [], "numFound": 0}}

    monkeypatch.setattr(archive.http, "fetch_json", fake_fetch)
    archive.search("collection:ravetapes", page=0, rows=999999)
    assert "page=1" in seen["url"]
    assert "rows=500" in seen["url"]
    archive.search("collection:ravetapes", page="nope", rows="nope")
    assert "page=1" in seen["url"]
    assert "rows=50" in seen["url"]
