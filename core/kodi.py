# -*- coding: utf-8 -*-
"""Kodi shim for the web build.

The provider modules are shared with the Kodi add-on, where they call into
`kodi` for logging and for the profile directory. Running them under Flask
only needs those two things, so this module provides them with plain Python
equivalents — no Kodi required.
"""

import logging
import os

LOG = logging.getLogger("raveweb")

DATA_DIR = os.environ.get("RAVEWEB_DATA") or "/tmp/raveweb"
try:  # a read-only data dir must not stop the providers from importing
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass

ICON = "static/art/icon.png"
FANART = "static/art/fanart.jpg"
ADDON_ID = "raveplayer.web"
ADDON_NAME = "Rave Player"
ADDON_VERSION = "3.6.0"
ADDON_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Addon(object):
    def getAddonInfo(self, key):
        return {
            "id": ADDON_ID,
            "name": ADDON_NAME,
            "version": ADDON_VERSION,
            "path": ADDON_PATH,
            "profile": DATA_DIR,
            "icon": ICON,
            "fanart": FANART,
        }.get(key, "")


ADDON = _Addon()


def log(message, level=10):
    if level >= 40:
        LOG.error(message)
    elif level >= 30:
        LOG.warning(message)
    elif level >= 20:
        LOG.info(message)
    else:
        LOG.debug(message)


def notify(*args, **kwargs):
    log("notify: %s" % (args,))
