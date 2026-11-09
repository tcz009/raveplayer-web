# -*- coding: utf-8 -*-
"""DejaVu FM warm feed cache: stale-while-revalidate and one-shot warm.

The Podomatic feed is ~10 MB and 4,000+ episodes, so a cold load is an
11-20 s request on the NAS.  Everything here is offline: the loader is a fake
that counts calls and can be held open, and each test gets its own cache
directory and process-local scheduling state.
"""

import json
import threading
import time

import pytest

import app as app_module
from core import cache, feeds

ENTRY = next(row for row in feeds.registry() if row["id"] == "dejavu-fm")
KEY = feeds._episode_key(ENTRY)


@pytest.fixture(autouse=True)
def _isolated_feed_warm(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEPLAYER_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear_memory()
    feeds._reset_state()
    yield
    # A failed assertion can leave the fake loader blocked; let the daemon
    # worker unwind before the next test reuses the module state.
    for state in list(feeds._WARM_STATES.values()):
        if state.refresh_active:
            state.done.wait(10)
    feeds._reset_state()
    cache.clear_memory()


def _rows(label="Old"):
    return [{"title": label, "url": "https://cdn.example/%s.mp3" % label.lower()}]


def _seed(rows=None):
    rows = _rows() if rows is None else rows
    assert cache.put(KEY, rows, sync=False) is True
    return rows


def _age_cache(age=None):
    """Rewrite the on-disk timestamp, then drop the in-process copy."""
    age = (feeds.WARM_REVALIDATE_AFTER + 60) if age is None else age
    path = cache.path_for(KEY)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["ts"] = time.time() - age
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    cache.clear_memory()


def _state():
    return feeds._WARM_STATES.get(KEY)


def _wait_for_refresh(timeout=5):
    state = _state()
    assert state is not None, "no refresh state was created"
    assert state.done.wait(timeout), "the background refresh did not finish"


def _refresh_threads():
    return [thread for thread in threading.enumerate()
            if thread.name == "feed-warm-dejavu-fm"]


def _fake_loader(rows, calls, delay=0.0, entered=None, release=None):
    def load(entry):
        calls.append(entry["id"])
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(5)
        if delay:
            time.sleep(delay)
        return rows
    return load


# --------------------------------------------------------------------- hot path


def test_cached_feed_is_served_without_reloading(monkeypatch):
    rows = _seed()
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader(_rows("Fresh"), calls))

    started = time.perf_counter()
    served = feeds.episodes("dejavu-fm")
    elapsed = time.perf_counter() - started

    assert served == rows
    assert calls == [], "a warm feed must not touch upstream"
    assert elapsed < 0.5, "a cache hit should be milliseconds, got %.3fs" % elapsed
    assert _state() is None, "a fresh cache must not schedule a refresh"


def test_parsed_feed_lasts_a_day_not_the_short_feed_ttl(monkeypatch):
    rows = _seed()
    # Well past the old 15-minute FEED_TTL, still before the 70% SWR mark.
    _age_cache(feeds.FEED_TTL * 2)
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader(_rows("Fresh"), calls))

    assert feeds.episodes("dejavu-fm") == rows
    assert calls == [], "the 24 h warm entry must survive the 15 min TTL"
    assert _state() is None, "below the 70% mark there is nothing to refresh"


def test_stale_feed_is_served_immediately_and_refreshed_once(monkeypatch):
    stale_rows = _seed()
    _age_cache()
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes",
                        _fake_loader(_rows("Fresh"), calls, delay=1.0))

    started = time.perf_counter()
    first = feeds.episodes("dejavu-fm")
    elapsed = time.perf_counter() - started

    assert first == stale_rows, "the stale list is served as-is"
    assert elapsed < 0.75, "a stale feed must not wait for the download, got %.3fs" % elapsed

    _wait_for_refresh()
    assert calls == ["dejavu-fm"]
    assert feeds.episodes("dejavu-fm") == _rows("Fresh")
    assert cache.age(KEY) < feeds.WARM_REVALIDATE_AFTER
    assert calls == ["dejavu-fm"]


def test_concurrent_stale_requests_start_exactly_one_refresh(monkeypatch):
    _seed()
    _age_cache()
    calls = []
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(feeds, "_load_episodes",
                        _fake_loader(_rows("Fresh"), calls,
                                     entered=entered, release=release))
    results = []
    errors = []
    barrier = threading.Barrier(8)

    def request():
        barrier.wait(5)
        try:
            results.append(feeds.episodes("dejavu-fm"))
        except Exception as exc:  # pragma: no cover - would fail the asserts
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert entered.wait(2), "the single refresh never started"
    time.sleep(0.15)  # give a would-be duplicate scheduler time to misfire
    assert calls == ["dejavu-fm"], "eight room loads must share one refresh"
    assert len(_refresh_threads()) == 1, "refresh threads must be deduped"
    assert not errors
    assert len(results) == 8

    release.set()
    _wait_for_refresh()
    assert calls == ["dejavu-fm"]


def test_many_requests_while_refresh_runs_do_not_spawn_more_threads(monkeypatch):
    _seed()
    _age_cache()
    calls = []
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(feeds, "_load_episodes",
                        _fake_loader(_rows("Fresh"), calls,
                                     entered=entered, release=release))

    assert feeds.episodes("dejavu-fm") == _rows()
    assert entered.wait(2)
    for _ in range(40):
        assert feeds.episodes("dejavu-fm") == _rows()
    assert calls == ["dejavu-fm"]
    assert len(_refresh_threads()) == 1

    release.set()
    _wait_for_refresh()
    assert calls == ["dejavu-fm"]


# ----------------------------------------------------------------- failure paths


def test_failed_refresh_keeps_stale_cache_and_obeys_the_retry_cooldown(monkeypatch):
    rows = _seed()
    _age_cache()
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader(None, calls))

    assert feeds.episodes("dejavu-fm") == rows
    _wait_for_refresh()
    assert calls == ["dejavu-fm"]

    # Still stale, but the cooldown suppresses a retry on every page load.
    assert feeds.episodes("dejavu-fm") == rows
    time.sleep(0.05)
    assert calls == ["dejavu-fm"]
    assert cache.age(KEY) > feeds.WARM_REVALIDATE_AFTER

    # Once the cooldown passes a later request may try again.
    state = _state()
    state.last_attempt = time.time() - feeds.WARM_RETRY_SECONDS - 1
    assert feeds.episodes("dejavu-fm") == rows
    _wait_for_refresh()
    assert calls == ["dejavu-fm", "dejavu-fm"]


def test_empty_upstream_parse_cannot_replace_a_good_stale_list(monkeypatch):
    rows = _seed()
    _age_cache()
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader([], calls))

    assert feeds.episodes("dejavu-fm") == rows
    _wait_for_refresh()
    assert calls == ["dejavu-fm"]
    # The refresh parsed zero rows, so the previous good list is still served.
    assert feeds.episodes("dejavu-fm") == rows
    assert cache.age(KEY) > feeds.WARM_REVALIDATE_AFTER


def test_cold_failure_returns_empty_without_crashing(monkeypatch):
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader(None, calls))

    assert feeds.episodes("dejavu-fm") == []
    assert calls == ["dejavu-fm"]


# --------------------------------------------------------------------- warm path


def test_warm_starts_one_background_load_without_blocking(monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(feeds, "_load_episodes",
                        _fake_loader(_rows("Fresh"), calls,
                                     entered=entered, release=release))

    started = time.perf_counter()
    assert feeds.warm() is True
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, "warm must only start a thread, got %.3fs" % elapsed
    assert entered.wait(2)
    assert feeds.warm() is False, "warm is once per process"
    assert calls == ["dejavu-fm"]

    release.set()
    _wait_for_refresh()
    assert feeds.episodes("dejavu-fm") == _rows("Fresh")
    assert calls == ["dejavu-fm"]


def test_warm_skips_a_fresh_cache(monkeypatch):
    _seed()
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader(_rows("Fresh"), calls))

    assert feeds.warm() is False
    time.sleep(0.05)
    assert calls == [], "a fresh cache needs no warm download"


def test_warm_offline_attempts_once_and_does_not_retry_the_process(monkeypatch):
    calls = []
    monkeypatch.setattr(feeds, "_load_episodes", _fake_loader(None, calls))

    assert feeds.warm() is True
    _wait_for_refresh()
    assert calls == ["dejavu-fm"]
    assert feeds.warm() is False
    time.sleep(0.05)
    assert calls == ["dejavu-fm"], "an offline NAS must not retry on every request"


def test_cold_concurrent_requests_share_one_upstream_load(monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(feeds, "_load_episodes",
                        _fake_loader(_rows("Fresh"), calls,
                                     entered=entered, release=release))
    results = []
    barrier = threading.Barrier(4)

    def request():
        barrier.wait(5)
        results.append(feeds.episodes("dejavu-fm"))

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()

    assert entered.wait(2), "the cold load never reached upstream"
    time.sleep(0.1)  # the other three must queue on the load lock, not reload
    assert calls == ["dejavu-fm"], "a cold room load must be single-flighted"

    release.set()
    for thread in threads:
        thread.join(5)
    assert len(results) == 4
    assert all(rows == _rows("Fresh") for rows in results)
    assert calls == ["dejavu-fm"]


# ---------------------------------------------------------------- request hook


def test_request_hook_is_disabled_in_the_test_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.feeds, "warm", lambda: calls.append(1))
    app_module._request_feed_warm()
    assert calls == [], "tests must never trigger the real 10 MB download"


def test_request_hook_warms_outside_the_test_environment(monkeypatch):
    monkeypatch.delenv("RAVEWEB_ENV", raising=False)
    monkeypatch.setitem(app_module.app.config, "TESTING", False)
    calls = []
    monkeypatch.setattr(app_module.feeds, "warm", lambda: calls.append(1))
    app_module._request_feed_warm()
    assert calls == [1]
