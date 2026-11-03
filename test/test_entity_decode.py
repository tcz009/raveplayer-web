# -*- coding: utf-8 -*-
"""Scraped titles must render as text, never as HTML entities (1.6.43).

Feeds and scrapers regularly double-encode an entity: a title that should read
``A & B – C`` arrives as ``A &amp;amp; B &amp;#x2013; C``.  These fixtures pin
the decoded output for Toronto, Moxser, Hardscore and the shared feed parser.
"""

import os

from core import feeds, hardscore, moxser, toronto

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "entities")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def assert_plain(value):
    assert "&" in value
    assert "\u2013" in value
    for entity in ("&amp;", "&#x2013;", "&quot;", "&#8211;"):
        assert entity not in value


def test_toronto_title_is_decoded():
    rows = toronto.parse_page(fixture("toronto.html"))
    assert len(rows) == 1
    assert rows[0]["title"] == "Brockie & DJ Rap \u2013 Jungle Mania"
    assert_plain(rows[0]["title"])


def test_moxser_title_is_decoded():
    rows = moxser._sitemap_entries(fixture("moxser_sitemap.xml"))
    assert len(rows) == 1
    assert rows[0]["title"] == "Alpha Show & Guest \u2013 26 October 2026"
    assert_plain(rows[0]["title"])


def test_hardscore_feed_title_is_decoded():
    episodes = feeds.parse(fixture("hardscore.xml"))
    assert len(episodes) == 1
    assert episodes[0]["title"] == (
        "Force of One Crew & MC \u2013 Weekend Rush [June 1993]")
    assert_plain(episodes[0]["title"])


def test_shared_feed_title_and_description_are_decoded():
    episodes = feeds.parse(fixture("feed.xml"))
    assert len(episodes) == 1
    assert episodes[0]["title"] == (
        'Skream & Benga \u2013 FWD "Rinse" 20.11.2009')
    assert_plain(episodes[0]["title"])
    assert "Live & direct \u2013 two hours" in episodes[0]["description"]


def test_hardscore_module_has_no_entity_helper_left_behind():
    """The title path is shared, so the module must not decode on its own."""
    assert not hasattr(hardscore, "_decode")
