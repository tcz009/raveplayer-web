# -*- coding: utf-8 -*-
"""In-process cache layer tests (web 1.6.18 warm-path speedup)."""

import json
import os

from core import cache


def test_memory_hit_works_after_the_disk_entry_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    assert cache.put("bench:key", {"value": 1}) is True
    path = cache.path_for("bench:key")
    os.remove(path)  # only the in-process copy remains
    assert cache.get("bench:key", 60) == {"value": 1}
    cache.clear_memory()
    assert cache.get("bench:key", 60) is cache.MISSING


def test_memory_values_are_copied_not_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    cache.put("bench:mutate", {"items": [1, 2]})
    first = cache.get("bench:mutate", 60)
    first["items"].append(3)
    assert cache.get("bench:mutate", 60) == {"items": [1, 2]}


def test_memory_layer_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cache, "MAX_MEMORY_ENTRIES", 2)
    cache.clear_memory()
    for index in range(3):
        cache.put("bench:bounded:%d" % index, index)
    keys = list(cache._MEMORY.keys())
    assert len(keys) == 2
    assert "bench:bounded:0" not in keys


def test_has_matches_get_for_fresh_and_stale_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    assert cache.has("bench:has", 60) is False
    cache.put("bench:has", {"value": 1})
    assert cache.has("bench:has", 60) is True
    assert cache.has("bench:has", 1, now=cache.time.time() + 10) is False


def test_shared_read_skips_the_deep_copy_for_read_only_callers(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    cache.put("bench:shared", {"items": [1, 2]})
    shared = cache.get("bench:shared", 60, copy_value=False)
    assert shared is cache.get("bench:shared", 60, copy_value=False)
    # The default stays defensive: a caller mutation cannot leak into the cache.
    copied = cache.get("bench:shared", 60)
    copied["items"].append(3)
    assert cache.get("bench:shared", 60)["items"] == [1, 2]
    assert cache.get("bench:shared", 60) is not cache.get("bench:shared", 60)
