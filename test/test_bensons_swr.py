# -*- coding: utf-8 -*-
"""20bensons stale-while-revalidate and one-shot warm behaviour.

The homepage is a slow scrape (25 s budget) and a Kodi room load must never
wait for it once any good list exists.  Everything here is offline: the
upstream fetch is a fake that counts calls and can be held open, and each test
gets its own cache directory and process-local refresh state.
"""

import json
import os
import threading
import time

import pytest

from core import bensons, cache

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "more-sources", "20bensons-home.html")


@pytest.fixture(autouse=True)
def _isolated_bensons(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    bensons._reset_state()
    yield
    # A failed assertion can leave the fake upstream blocked; let the daemon
    # worker unwind before the next test reuses the module state.
    if bensons._REFRESH_ACTIVE:
        bensons._REFRESH_DONE.wait(10)
    bensons._reset_state()
    cache.clear_memory()


def _page():
    with open(FIXTURE, encoding="utf-8") as handle:
        return handle.read()


def _rows():
    return bensons.parse_homepage(_page())


def _urls(rows):
    return [row["url"] for row in rows]


def _seed(rows=None):
    rows = _rows() if rows is None else rows
    assert cache.put(bensons.CACHE_KEY, rows, sync=False) is True
    return rows


def _age_cache(age=None):
    """Rewrite the on-disk timestamp, then drop the in-process copy."""
    age = (bensons.REVALIDATE_AFTER + 60) if age is None else age
    path = cache.path_for(bensons.CACHE_KEY)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["ts"] = time.time() - age
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    cache.clear_memory()


def _refresh_threads():
    return [thread for thread in threading.enumerate()
            if thread.name == "bensons-refresh"]


def _fake_fetch(page, calls, delay=0.0):
    def fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append({"url": url, "ttl": ttl})
        if delay:
            time.sleep(delay)
        return page
    return fetch


# --------------------------------------------------------------------- hot path


def test_cache_hit_is_served_without_any_upstream_call(monkeypatch):
    rows = _seed()
    calls = []
    monkeypatch.setattr(bensons.http, "fetch", _fake_fetch(_page(), calls))

    started = time.perf_counter()
    served = bensons.entries()
    elapsed = time.perf_counter() - started

    assert _urls(served) == _urls(rows)
    assert calls == [], "a warm /api/20bensons must not touch upstream"
    assert elapsed < 0.5, "a cache hit should be milliseconds, got %.3fs" % elapsed


def test_stale_cache_is_served_immediately_and_refreshed_in_the_background(monkeypatch):
    stale_rows = _seed()
    _age_cache()
    calls = []
    refreshed_page = _page().replace(
        "TOP BUZZ FANTAZIA NYE92 A-SIDE.MP3",
        "TOP BUZZ FANTAZIA NYE92 A-SIDE-REFRESHED.MP3")
    monkeypatch.setattr(bensons.http, "fetch",
                        _fake_fetch(refreshed_page, calls, delay=1.0))

    started = time.perf_counter()
    first = bensons.entries()
    elapsed = time.perf_counter() - started

    assert _urls(first) == _urls(stale_rows), "the stale list is served as-is"
    assert first[0]["title"] == "TOP BUZZ FANTAZIA NYE92 A-SIDE"
    assert elapsed < 0.75, "a stale cache must not wait for the scrape, got %.3fs" % elapsed

    assert bensons._REFRESH_DONE.wait(5), "the background refresh did not finish"
    assert len(calls) == 1
    assert calls[0]["ttl"] == 0, "the refresh must bypass the HTTP body cache"

    fresh = bensons.entries()
    assert fresh[0]["title"] == "TOP BUZZ FANTAZIA NYE92 A-SIDE-REFRESHED"
    assert cache.age(bensons.CACHE_KEY) < bensons.REVALIDATE_AFTER
    assert len(calls) == 1


def test_concurrent_stale_requests_start_exactly_one_refresh(monkeypatch):
    _seed()
    _age_cache()
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return _page()

    monkeypatch.setattr(bensons.http, "fetch", slow_fetch)
    results = []
    errors = []
    barrier = threading.Barrier(8)

    def request():
        barrier.wait(5)
        try:
            results.append(bensons.entries())
        except Exception as exc:  # pragma: no cover - would fail the asserts
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert entered.wait(2), "the single refresh never started"
    time.sleep(0.15)  # give a would-be duplicate scheduler time to misfire
    assert len(calls) == 1, "eight room loads must share one refresh"
    assert len(_refresh_threads()) == 1, "refresh threads must be deduped"
    assert not errors
    assert len(results) == 8
    assert all(len(rows) == 234 for rows in results)

    release.set()
    assert bensons._REFRESH_DONE.wait(5)
    assert len(calls) == 1


def test_many_requests_while_refresh_runs_do_not_spawn_more_threads(monkeypatch):
    _seed()
    _age_cache()
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return _page()

    monkeypatch.setattr(bensons.http, "fetch", slow_fetch)
    assert len(bensons.entries()) == 234
    assert entered.wait(2)

    for _ in range(40):
        assert len(bensons.entries()) == 234
    assert len(calls) == 1
    assert len(_refresh_threads()) == 1

    release.set()
    assert bensons._REFRESH_DONE.wait(5)
    assert len(calls) == 1


# ----------------------------------------------------------------- failure paths


def test_failed_refresh_keeps_stale_cache_and_obeys_the_retry_cooldown(monkeypatch):
    rows = _seed()
    _age_cache()
    calls = []

    def failing_fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append(url)
        return None

    monkeypatch.setattr(bensons.http, "fetch", failing_fetch)

    assert _urls(bensons.entries()) == _urls(rows)
    assert bensons._REFRESH_DONE.wait(5)
    assert len(calls) == 1

    # Still stale, but the cooldown suppresses a retry on every page load.
    assert _urls(bensons.entries()) == _urls(rows)
    time.sleep(0.05)
    assert len(calls) == 1
    assert cache.age(bensons.CACHE_KEY) > bensons.REVALIDATE_AFTER

    # Once the cooldown passes a later request may try again.
    bensons._REFRESH_LAST_ATTEMPT = (time.time()
                                     - bensons.REFRESH_RETRY_SECONDS - 1)
    assert _urls(bensons.entries()) == _urls(rows)
    assert bensons._REFRESH_DONE.wait(5)
    assert len(calls) == 2


def test_empty_upstream_parse_cannot_replace_a_good_stale_list(monkeypatch):
    rows = _seed()
    _age_cache()
    calls = []
    monkeypatch.setattr(bensons.http, "fetch",
                        _fake_fetch("<html><body>maintenance</body></html>", calls))

    assert _urls(bensons.entries()) == _urls(rows)
    assert bensons._REFRESH_DONE.wait(5)
    assert len(calls) == 1
    # The refresh parsed zero rows, so the previous good list is still served.
    assert _urls(bensons.entries()) == _urls(rows)
    assert cache.age(bensons.CACHE_KEY) > bensons.REVALIDATE_AFTER


# --------------------------------------------------------------------- warm path


def test_warm_starts_one_background_refresh_without_blocking(monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return _page()

    monkeypatch.setattr(bensons.http, "fetch", slow_fetch)

    started = time.perf_counter()
    assert bensons.warm() is True
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, "warm must only start a thread, got %.3fs" % elapsed
    assert entered.wait(2)
    assert bensons.warm() is False, "warm is once per process"
    assert len(calls) == 1

    release.set()
    assert bensons._REFRESH_DONE.wait(5)
    assert len(bensons.entries()) == 234
    assert len(calls) == 1


def test_warm_skips_a_fresh_cache(monkeypatch):
    _seed()
    calls = []
    monkeypatch.setattr(bensons.http, "fetch", _fake_fetch(_page(), calls))

    assert bensons.warm() is False
    time.sleep(0.05)
    assert calls == [], "a fresh cache needs no warm scrape"


def test_warm_offline_attempts_once_and_does_not_retry_the_process(monkeypatch):
    calls = []

    def failing_fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append(url)
        return None

    monkeypatch.setattr(bensons.http, "fetch", failing_fetch)

    assert bensons.warm() is True
    assert bensons._REFRESH_DONE.wait(5)
    assert len(calls) == 1
    assert bensons.warm() is False
    time.sleep(0.05)
    assert len(calls) == 1, "an offline NAS must not retry on every request"


def test_cold_concurrent_requests_share_one_upstream_load(monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(url, timeout=0, headers=None, ttl=0, max_bytes=None):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return _page()

    monkeypatch.setattr(bensons.http, "fetch", slow_fetch)
    results = []
    barrier = threading.Barrier(4)

    def request():
        barrier.wait(5)
        results.append(bensons.entries())

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()

    assert entered.wait(2), "the cold load never reached upstream"
    time.sleep(0.1)  # the other three must queue on the load lock, not fetch
    assert len(calls) == 1, "a cold room load must be single-flighted"

    release.set()
    for thread in threads:
        thread.join(5)
    assert len(results) == 4
    assert all(len(rows) == 234 for rows in results)
    assert len(calls) == 1
