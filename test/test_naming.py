# -*- coding: utf-8 -*-
"""Artwork naming rules for the web build (1.6.43).

Every sticker filename is lowercase kebab-case (``[a-z0-9]+(-[a-z0-9]+)*``);
``ukg`` is spelled ``uk-garage`` everywhere a file, route or test refers to it.
"""

import os
import re

import app as app_module

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ART = os.path.join(ROOT, "static", "art")

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.(?:png|jpg)$")
APP_REF = re.compile(r'"([A-Za-z0-9_. -]+\.(?:png|jpg))"')
JS_REF = re.compile(r"/static/art/([A-Za-z0-9_. -]+\.(?:png|jpg))")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_every_art_file_is_kebab_case():
    for name in sorted(os.listdir(ART)):
        if not os.path.isfile(os.path.join(ART, name)):
            continue
        assert NAME.match(name), name


def test_every_app_art_reference_is_kebab_case():
    for name in APP_REF.findall(_read(os.path.join(ROOT, "app.py"))):
        assert NAME.match(name), name
    for name in JS_REF.findall(_read(os.path.join(ROOT, "static", "app.js"))):
        assert NAME.match(name), name


def test_every_referenced_art_exists():
    names = set(APP_REF.findall(_read(os.path.join(ROOT, "app.py"))))
    names.update(JS_REF.findall(_read(os.path.join(ROOT, "static", "app.js"))))
    missing = sorted(name for name in names
                     if not os.path.exists(os.path.join(ART, name)))
    assert missing == []


def test_uk_garage_is_spelled_out():
    assert "uk-garage" in dict((row[0], row) for row in app_module.RADIO_SECTIONS)
    assert "ukg" not in dict((row[0], row) for row in app_module.RADIO_SECTIONS)
    assert app_module.SECTION_PATHS["uk-garage"] == "/api/live/uk-garage"
    assert "ukg" not in app_module.SECTION_PATHS
    assert app_module.LIVE_ART["uk-garage"] == "radio-uk-garage.png"
    assert "ukg" not in app_module.LIVE_ART


def test_ukg_route_is_only_a_legacy_alias(web):
    assert web.get("/api/uk-garage").status_code == 200
    legacy = web.get("/api/ukg")
    assert legacy.status_code == 301
    assert legacy.headers["Location"].endswith("/api/uk-garage")
