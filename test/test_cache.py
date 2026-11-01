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
