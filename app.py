# -*- coding: utf-8 -*-
"""Rave Player — self-hosted web build.

Same content sources as the Kodi add-on (Internet Archive, radio-browser,
podcast feeds, DNBRADIO, the surviving legacy sites), served as a small
Flask app so it can run in Docker on a home server.

Two things make the browser work where an add-on does not:
* a server-side stream proxy, because some sources are hotlink-protected and
  because mixed-content/CORS would otherwise block playback, and
* one JSON endpoint per list, so the UI stays a thin client.

The stream proxy is the security-sensitive part.  It only ever talks HTTP(S),
resolves every hostname itself, validates *all* addresses, and then connects
to the validated address while keeping the original hostname for the Host
header, TLS SNI and certificate checks.  Redirects are followed by hand so
every hop goes through the same checks.  Concurrency is capped so a pile of
slow streams cannot take every gunicorn thread away from the API.
"""

import base64
import datetime
import http.client
import ipaddress
import json
import math
import os
import random
import re
import socket
import ssl
import tempfile
import threading
import time
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse

import gzip
import secrets

from flask import (Flask, Response, g, jsonify, redirect, render_template, request,
                   send_file, session, stream_with_context, url_for)
from werkzeug.exceptions import HTTPException
from flask.sessions import SecureCookieSessionInterface

from core import (archive, audit, auth, bensons, cache, demodulated, dnbradio,
                  download_cancels, download_jobs, downloads, drive, ezgarage,
                  favourites, feeds,
                  fetch as fetch_core, getdarker, hardscore, latest, legacy,
                  mikus, mixcloud, moxser, pirate, playback, radio, raves,
                  ravearchive, soundcloud, toronto, ukraves)

app = Flask(__name__, static_folder="static", template_folder="templates")
from core.resolver import stream_budget  # noqa: E402
from core.resolver.service import resolver_bp  # noqa: E402
from core.resolver.mega_stream import (  # noqa: E402
    has_valid_token as mega_stream_has_valid_token,
    mega_stream_bp,
)
app.register_blueprint(resolver_bp)
app.register_blueprint(mega_stream_bp)
app.config["JSON_SORT_KEYS"] = False


@app.after_request
def _gzip_json(response):
    """Compress JSON for browsers that accept gzip (cold payloads get smaller).

    Streaming responses (the download NDJSON panel) are left alone so progress
    lines are not buffered by a compressor; tiny bodies are not worth it.
    """
    try:
        if request.method == "HEAD" or response.direct_passthrough or response.is_streamed:
            return response
        if response.headers.get("Content-Encoding"):
            return response
        if response.status_code != 200 or response.mimetype != "application/json":
            return response
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return response
        body = response.get_data()
        if len(body) < 1024:
            return response
        response.set_data(gzip.compress(body, 6))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(response.get_data()))
        response.headers.add("Vary", "Accept-Encoding")
    except Exception:  # noqa: BLE001 - compression is optional
        pass
    return response


def _read_version():
    """Single source of truth: RAVEWEB_VERSION env, else the VERSION file."""
    override = os.environ.get("RAVEWEB_VERSION", "").strip()
    if override:
        return override
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return "0.0.0"
    return value or "0.0.0"


APP_VERSION = _read_version()
app.json.sort_keys = False

# Single-operator login (1.6.20).  Fails closed: without a configured password
# (or SECRET_KEY in production) the app refuses to start.
try:
    AUTH = auth.load_config()
except auth.AuthConfigError as exc:
    raise RuntimeError("authentication configuration error: %s" % exc)
app.secret_key = AUTH.secret_key
class _HttpsAwareSessionInterface(SecureCookieSessionInterface):
    """Mark the session cookie Secure when the request arrived over HTTPS.

    Flask saves the session *after* ``after_request`` handlers run, so the flag
    cannot be added by rewriting the response; override the cookie policy instead.
    """

    def get_cookie_secure(self, app):
        try:
            if request.is_secure:
                return True
            forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0]
            return forwarded.strip().lower() == "https"
        except RuntimeError:  # outside a request (e.g. CLI)
            return False


app.session_interface = _HttpsAwareSessionInterface()
app.config.update(
    SESSION_COOKIE_NAME="raveplayer_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # upgraded per-request when the request is HTTPS
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=AUTH.remember_days),
    SESSION_REFRESH_EACH_REQUEST=True,
)
LOGIN_LIMITER = auth.SqliteLoginLimiter(max_failures=AUTH.max_failures,
                                  failure_window=AUTH.failure_window,
                                  lockout_seconds=AUTH.lockout_seconds)
ADMIN_ACTION_LIMITER = auth.SqliteLoginLimiter(max_failures=30,
                                               failure_window=60.0,
                                               lockout_seconds=60.0)
_PLAYBACK_RATE = {"lock": threading.Lock(), "state": {}}
PLAYBACK_RATE_LIMIT = 60  # heartbeats per minute per session
if AUTH.generated_hash:
    app.logger.warning(
        "auth: owner RAVEWEB_PASSWORD was hashed for this run; persist it with "
        "RAVEWEB_PASSWORD_HASH=%s", AUTH.generated_hash)
if AUTH.generated_friends_hash:
    app.logger.warning(
        "auth: friends RAVEWEB_FRIENDS_PASSWORD was hashed for this run; persist "
        "it with RAVEWEB_FRIENDS_PASSWORD_HASH=%s", AUTH.generated_friends_hash)
if AUTH.production and AUTH.secret_key:
    app.logger.info("auth: login enabled (production mode)")
if not AUTH.friends_hash:
    app.logger.warning("auth: no friends credential configured; only owner can sign in")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

DATA_DIR = os.environ.get("RAVEWEB_DATA") or "/tmp/raveweb"
try:  # a read-only data dir must not stop the app from booting
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError as exc:  # pragma: no cover - depends on the host
    app.logger.warning("data dir %s is not writable at startup: %s", DATA_DIR, exc)

ALLOW_PRIVATE = os.environ.get("RAVEWEB_ALLOW_PRIVATE", "") == "1"
# Optional extra restriction for /api/stream: a comma-separated list of host
# suffixes. Empty (the default) keeps live radio working — the directory lists
# stations on arbitrary public hosts — while an operator can pin the proxy to
# the hosts the app actually uses (archive.org, rinse.fm, podcast CDNs, ...).
STREAM_HOST_ALLOWLIST = tuple(
    host.strip().lower().lstrip(".")
    for host in os.environ.get("RAVEWEB_STREAM_HOSTS", "").split(",")
    if host.strip()
)
USER_AGENT = "RavePlayer-Web/1.0 (+https://github.com/tcz009/plugin.audio.raveplayer)"
ART = "/static/art/"
CSP_REPORT_ONLY = (os.environ.get("RAVEWEB_CSP_REPORT_ONLY", "1") or "1") == "1"
CSP_REPORT_URI = "/api/csp-report"
CSP_REPORT_GROUP = "csp-endpoint"
CSP = (
    "default-src 'self'; img-src 'self' data: https: http:; media-src 'self'; "
    # The Mixcloud and SoundCloud sections play sets in the providers' own
    # iframes. Mixcloud's widget answers with a 301 to player-widget.mixcloud.com,
    # and CSP re-checks every redirect hop, so both Mixcloud hosts are allowed.
    "frame-src https://www.mixcloud.com https://player-widget.mixcloud.com https://w.soundcloud.com; "
    "script-src 'self'; style-src 'self'; connect-src 'self'; manifest-src 'self'; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
)

ROWS = 40
# JungleTapes has 300+ sitemap shows; the web list and the Kodi directory both
# page at 50 so the catalogue is browsable instead of one huge response.
MAX_PAGE = 500
MAX_QUERY = 200
MAX_URL = 2048
MAX_TITLE = 300
MIN_YEAR = 1987

# Stream proxy limits.  Every stream holds one worker thread, so this is the
# number that decides how much of a gunicorn worker a slow listener can take.
MAX_STREAMS = 6
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_CHUNK = 64 * 1024
STREAM_DEADLINE = 4 * 3600
MAX_STREAM_BYTES = 8 * 1024 * 1024 * 1024

# Downloads (1.6.16).  Phase 1 is explicit, direct-file only: the four source
# markers below are the only things the UI offers, and the server re-checks the
# URL against the source's hosts so a caller cannot label a Mixcloud page as an
# Archive MP3.  Count/size/free-space guards are enforced before and after the
# proxy download; the fetch reuses _open_stream (SSRF + redirect checks).
def _env_bytes(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


DOWNLOAD_MAX_BYTES = _env_bytes("RAVEWEB_DOWNLOAD_BYTES", 500 * 1024 * 1024,
                                1024 * 1024, 4 * 1024 * 1024 * 1024)
DOWNLOAD_MAX_TOTAL = _env_bytes("RAVEWEB_DOWNLOAD_TOTAL", 2 * 1024 * 1024 * 1024,
                                1024 * 1024, 16 * 1024 * 1024 * 1024)
DOWNLOAD_MAX_ITEMS = 200
DOWNLOAD_DEADLINE = 1800  # seconds for one proxy download
# RaveTapePacks' origin has repeatedly cut full-file responses mid-stream
# (QA 1.6.62).  Playback stays available; downloads are hidden until the
# integrity check has been verified against the live host.  Set
# RAVEWEB_RTP_DOWNLOADS=1 to re-enable the affordance after that check.
RTP_DOWNLOADS_ENABLED = (os.environ.get("RAVEWEB_RTP_DOWNLOADS", "0")
                         or "0").strip().lower() in ("1", "true", "yes", "on")
DOWNLOAD_SOURCES = ("archive", "moxser", "hardscore", "pirate", "toronto",
                    "ravetapepacks", "jungletapes", "ezgarage", "mediafire",
                    "20bensons", "ukraves", "mikus", "demodulated", "podomatic")
DOWNLOAD_EXTENSIONS = (".mp3", ".m4a")
DOWNLOAD_JOB_CONCURRENCY = max(1, min(2, int(os.environ.get("RAVEWEB_DOWNLOAD_JOBS", "2") or 2)))
DOWNLOAD_JOB_MAX = 20
DOWNLOAD_BLOCKED_HOSTS = (
    "mixcloud.com", "player-widget.mixcloud.com", "soundcloud.com",
    "w.soundcloud.com", "youtube.com", "youtu.be", "vimeo.com",
)
# Exact hosts only: the Drive resolver and Mikus Musik's own audio host.
DRIVE_HOSTS = ("drive.usercontent.google.com", "drive.google.com", "docs.google.com")
MIKUS_DIRECT_HOSTS = ("www.terraincognita.co.uk", "terraincognita.co.uk")
DOWNLOAD_SOURCE_LABELS = {
    "archive": "Internet Archive",
    "moxser": "Moxser",
    "hardscore": "Hardscore",
    "pirate": "Pirate Radio Archives",
    "toronto": "Toronto Rave Mixtape Archive",
    "ravetapepacks": "RaveTapePacks.com",
    "jungletapes": "JungleTapes.com",
    "ezgarage": "DNBForum / Mediafire",
    "mediafire": "DNBForum / Mediafire",
    "20bensons": "20bensons.com",
    "ukraves": "UK Raves",
    "mikus": "Mikus Musik",
    "demodulated": "Demodulated Mixtapes",
    "podomatic": "Podomatic",
}
DOWNLOAD_SOURCE_ART = {
    "archive": ART + "tapepacks-all.png",
    "moxser": ART + "moxser.png",
    "hardscore": ART + "hardscore.png",
    "pirate": ART + "pirate-archive.png",
    "toronto": ART + "toronto.png",
    "ravetapepacks": ART + "ravetapepacks.png",
    "jungletapes": ART + "jungletapes.png",
    "ezgarage": ART + "djez.png",
    "mediafire": ART + "djez.png",
    "20bensons": ART + "20bensons.png",
    "ukraves": ART + "ukraves.png",
    "mikus": ART + "mikus.png",
    "demodulated": ART + "demodulated.png",
    "podomatic": ART + "podcasts.png",
}

_REDIRECT_CODES = frozenset((301, 302, 303, 307, 308))
# Ports that are never plausible audio sources.  Privileged ports other than
# 80/443 are refused too: radio streams live on 8000+ and the Archive on 443.
_BLOCKED_PORTS = frozenset((
    21, 22, 23, 25, 53, 110, 111, 135, 137, 138, 139, 143, 445, 465, 587,
    993, 995, 1433, 1521, 2049, 2375, 2376, 3306, 3389, 5432, 5900, 6379,
    6443, 9200, 9300, 11211, 27017,
))
_ALLOWED_LOW_PORTS = frozenset((80, 443))
_EXTRA_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
))
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_RANGE_RE = re.compile(r"^bytes=\d{0,18}-\d{0,18}$")
_MEDIA_EXACT = frozenset((
    "application/ogg", "application/x-ogg", "application/octet-stream",
    "binary/octet-stream", "application/mp4", "application/x-m4a",
    "application/x-mpeg", "application/mpegurl", "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
))

_SSL_CONTEXT = ssl.create_default_context()

ARCHIVE_SECTIONS = [
    ("jungle", "Jungle", 'collection:ravetapes AND jungle AND mediatype:audio', "Jungle sets"),
    ("dnb", "Drum & Bass", 'collection:ravetapes AND ("drum and bass" OR dnb) AND mediatype:audio', "Drum & bass sets"),
    ("hardcore", "Hardcore", 'collection:ravetapes AND hardcore AND NOT "happy hardcore" AND mediatype:audio', "Hardcore sets"),
    ("happy-hardcore", "Happy Hardcore", 'collection:ravetapes AND "happy hardcore" AND mediatype:audio', "Happy hardcore sets"),
    ("house", "House", "collection:ravetapes AND house AND mediatype:audio", "House music sets"),
    ("techno", "Techno", "collection:ravetapes AND techno AND mediatype:audio", "Techno sets"),
    ("garage", "UK Garage", '(title:("uk garage") OR title:("garage nation") OR title:("pure garage") OR title:("speed garage")) AND mediatype:audio', "UK garage, 90s to now"),
    ("tapepacks", "Every Pack", "collection:ravetapes AND mediatype:audio", "Archive mixtapes"),
]

RADIO_SECTIONS = [
    ("hardcore", "Hardcore", "hardcore"),
    ("jungle", "Jungle", "jungle"),
    ("drum-and-bass", "Drum & Bass", "drum and bass"),
    ("house", "House", "house"),
    ("uk-garage", "UK Garage", "uk garage"),
    ("dubstep-grime", "Dubstep & Grime", "dubstep"),
]

SECTIONS = [
    ("latest", "Latest", "rave.png", "Newest of everything"),
    ("toronto", "Toronto Rave Mixtape Archive", "toronto.png", "Toronto rave tapes, 1990–2014"),
    ("jungletapes", "JungleTapes.com", "jungletapes.png", "Toronto jungle radio from cassette, 1996–2000"),
    ("ravetapepacks", "RaveTapePacks.com", "ravetapepacks.png", "UK tape packs, 1992–2004"),
    ("ezgarage", "EZ Garage Sets", "djez.png", "DJ EZ and old-skool garage sets from DNBForum"),
    ("20bensons", "20bensons.com", "20bensons.png", "234 old-skool rave MP3s, 1990–2005"),
    ("ukraves", "UK Raves Downloads", "ukraves.png", "Event downloads from UK Raves (Mediafire direct files)"),
    ("classic-packs", "Classic Pack Series", "series.png", "The UK pack canon"),
    ("by-rave", "By Rave, Venue & Promoter", "rave-nights.png", "Raves, venues and promoters"),
    ("by-year", "By Year", "years.png", "Tape packs by year"),
    ("every-pack", "Every Pack", "rave-tapes.png", "Everything tagged as a tape pack"),
    ("podcasts", "Podcasts & Mixes", "podcasts.png", "Present-day shows"),
    ("dnb", "DNBRADIO", "dnb.png", "Jungle and drum & bass shows"),
    ("getdarker", "GetDarker", "getdarker.png", "Dubstep and grime, 2003 onwards"),
    ("moxser", "Moxser", "moxser.png", "Old skool to new cool dance mixes"),
    ("mikus", "Mikus Musik", "mikus.png", "House, garage and jungle mixes from the blog"),
    ("demodulated", "Demodulated Mixtapes", "demodulated.png", "Toronto techno and rave mixtapes"),
    ("hardscore", "Hardscore", "hardscore.png", "1990s pirate-radio recordings"),
    ("pirates", "Pirate Radio Archives", "pirate-archive.png", "Curated pirate-radio recordings"),
    ("kool-sets", "Kool FM Sets", "kool.png", "Kool FM recordings"),
    ("live", "Live Radio", "radio.png", "Stations on air now"),
    ("hardcore", "Hardcore", "radio-hardcore.png", "Happy hardcore and rave stations on air"),
    ("jungle", "Jungle", "radio-jungle.png", "Jungle and ragga stations on air"),
    ("drum-and-bass", "Drum & Bass", "radio-dnb.png", "Drum & bass stations on air"),
    ("house", "House", "radio-house.png", "House stations on air"),
    ("uk-garage", "UK Garage", "radio-uk-garage.png", "UK garage and 2-step stations on air"),
    ("dubstep-grime", "Dubstep & Grime", "radio-dubstep.png", "Dubstep, grime and 140 stations on air"),
    ("favourites", "Favourites", "favourites.png", "The sets you saved"),
    ("downloads", "Downloads", "downloads.png", "Saved for offline"),
    ("search", "Search", "search.png", "Search every tape pack"),
]

# One distinct sticker per navigation row.
PACK_ART = {
    "jungle": "jungle.png",
    "dnb": "dnb.png",
    "hardcore": "hardcore.png",
    "happy-hardcore": "happy-hardcore.png",
    "house": "house.png",
    "techno": "techno.png",
    "garage": "garage.png",
    "tapepacks": "tapepacks-all.png",
}
FILTER_ART = {
    "kool": "kool.png",
    "rinse": "rinse.png",
    "dreamscape": "dreamscape.png",
    "helter-skelter": "helter-skelter.png",
    "fantazia": "fantazia.png",
    "one-nation": "one-nation.png",
    "centreforce": "centreforce.png",
    "don-fm": "don-fm.png",
    "slammin-vinyl": "slammin-vinyl.png",
    "one-in-the-jungle": "one-in-the-jungle.png",
    "jungle-fever": "jungle-fever.png",
    "roast": "roast.png",
    "telepathy": "telepathy.png",
    "world-dance": "world-dance.png",
    "dj-ez": "djez.png",
    "garage-nation": "garage-nation.png",
}

LIVE_ART = {
    "hardcore": "radio-hardcore.png",
    "jungle": "radio-jungle.png",
    "drum-and-bass": "radio-dnb.png",
    "house": "radio-house.png",
    "uk-garage": "radio-uk-garage.png",
    "dubstep-grime": "radio-dubstep.png",
}

SECTION_PATHS = {
    "latest": "/api/latest",
    "toronto": "/api/toronto",
    "jungletapes": "/api/legacy/jungletapes",
    "ravetapepacks": "/api/legacy/ravetapepacks",
    "ezgarage": "/api/ezgarage",
    "20bensons": "/api/20bensons",
    "ukraves": "/api/ukraves",
    "classic-packs": "/api/filters",
    "by-rave": "/api/raves",
    "by-year": "/api/years",
    "every-pack": "/api/every-pack",
    "podcasts": "/api/podcasts",
    "dnb": "/api/dnb",
    "getdarker": "/api/getdarker",
    "moxser": "/api/moxser",
    "mikus": "/api/mikus",
    "demodulated": "/api/demodulated",
    "hardscore": "/api/hardscore",
    "pirates": "/api/pirate",
    "kool-sets": "/api/filter/kool",
    "live": "/api/live",
    "hardcore": "/api/live/hardcore",
    "jungle": "/api/live/jungle",
    "drum-and-bass": "/api/live/drum-and-bass",
    "house": "/api/live/house",
    "uk-garage": "/api/live/uk-garage",
    "dubstep-grime": "/api/live/dubstep-grime",
    "favourites": "/api/favourites",
    "downloads": "/api/downloads",
    "search": "/api/search",
}

# 1.6.44 taxonomy: tape packs → pirate radio → shows → live → saved.
RAIL = [
    {"id": "latest", "label": "Latest", "icon": "rave.png", "path": "/api/latest",
     "children": ["latest"], "sub": "Newest packs, shows and radio rips."},
    {"id": "packs", "label": "Tape Packs", "icon": "tapepacks.png",
     "children": ["toronto", "jungletapes", "ravetapepacks", "ezgarage",
                  "20bensons", "ukraves", "classic-packs", "by-rave",
                  "by-year", "every-pack"],
     "sub": "Cassette packs and rave tapes, by event, DJ, MC and year."},
    {"id": "pirate", "label": "Pirate Radio", "icon": "pirate-radio.png",
     "children": ["hardscore", "pirates", "kool-sets"],
     "sub": "1990s pirate-radio recordings: Kool FM, Don FM, Green Apple."},
    {"id": "shows", "label": "Mixes & Shows", "icon": "mixes.png",
     "children": ["podcasts", "dnb", "getdarker", "moxser", "mikus",
                  "demodulated"],
     "sub": "Podcasts, DNBRADIO, GetDarker and studio mixes."},
    {"id": "live", "label": "Live Radio", "icon": "radio.png",
     "path": "/api/live",
     "children": ["live", "hardcore", "jungle", "drum-and-bass", "house",
                  "uk-garage", "dubstep-grime"], "sub": "Stations on air now."},
    {"id": "favourites", "label": "Favourites", "icon": "favourites.png",
     "path": "/api/favourites", "children": ["favourites"], "sub": "The sets you saved."},
    {"id": "downloads", "label": "Downloads", "icon": "downloads.png",
     "path": "/api/downloads", "children": ["downloads"], "sub": "Saved for offline."},
]


# --------------------------------------------------------------------------- helpers


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


MAX_STREAMS = _env_int("RAVEWEB_MAX_STREAMS", MAX_STREAMS, 1, 64)
STREAM_DEADLINE = _env_int("RAVEWEB_STREAM_SECONDS", STREAM_DEADLINE, 30, 24 * 3600)
MAX_STREAM_BYTES = _env_int("RAVEWEB_STREAM_BYTES", MAX_STREAM_BYTES, 1 << 20, 1 << 40)
_STREAM_SLOTS = threading.BoundedSemaphore(MAX_STREAMS)


def _filters():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filters.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return [entry for entry in data if isinstance(entry, dict) and entry.get("id")]


def _filter(key):
    return next((entry for entry in _filters() if entry["id"] == key), None)


def _track(label, sub, play_url, art=None, fav=None):
    item = {"label": label, "sub": sub, "play": play_url, "art": art or (ART + "radio.png")}
    if fav:
        item["fav"] = fav
    return item


def _folder(label, sub, api, art=None, fav=None):
    item = {"label": label, "sub": sub, "open": api, "art": art or (ART + "tapepacks.png")}
    if fav:
        item["fav"] = fav
    return item


def _http_url(value):
    """Only http(s) leaves this process as a link/stream (no javascript: etc.)."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return ""


def _external_row(label, sub, url, art=None):
    """A row that opens a source page in a new tab (SPA external fallback)."""
    item = _track(label, sub, None, art)
    item["external"] = url
    return item


def _source_sub(*parts):
    return " · ".join(str(part) for part in parts if part)


def _year_label(doc):
    year = doc.get("year") or str(doc.get("date") or "")[:4]
    return str(year) if year else ""


# Display-only duration cap (24 h): one hostile Archive/feed value must never
# turn a list row into a 500.  Parsing is bounded in core/archive.py and
# core/feeds.py too; this is the last line of defence for a huge int that
# reaches the view (float(huge_int) raises OverflowError on Python 3.9).
MAX_DURATION_SECONDS = 24 * 60 * 60


def _human_duration(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(number) or number > MAX_DURATION_SECONDS:
        return ""
    try:
        seconds = int(number)
    except (OverflowError, ValueError):  # pragma: no cover - covered by float()
        return ""
    if seconds < 0:
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%d:%02d" % (minutes, seconds)


def _clean_text(value, limit):
    """Return a single-line string with control characters removed."""
    if not isinstance(value, str):
        return ""
    text = _CTRL_RE.sub(" ", value).strip()
    return text[:limit]


def _page_arg():
    """Page number from the query string, clamped to a sane range."""
    page = request.args.get("page", type=int)
    if page is None or page < 1:
        return 1
    return min(page, MAX_PAGE)


def _next_link(base, page, total):
    if page >= MAX_PAGE or total <= page * ROWS:
        return None
    return _page_link(base, page + 1)


def _page_link(base, page):
    """Build ``?page=N`` without losing an existing query string."""
    separator = "&" if "?" in base else "?"
    return "%s%spage=%d" % (base, separator, max(1, int(page)))


def _page_numbers(page, pages, window=2):
    """Numbers for the numbered pager: 1 … 4 5 6 … N (None = ellipsis)."""
    if pages <= 0:
        return []
    wanted = {1, pages}
    wanted.update(range(max(1, page - window), min(pages, page + window) + 1))
    numbers, last = [], 0
    for number in sorted(wanted):
        if last and number > last + 1:
            numbers.append(None)
        numbers.append(number)
        last = number
    return numbers


def _pager(page, total, base, per_page=ROWS):
    """Uniform pager metadata for every web list.

    ``links`` is the numbered sequence (with ``None`` where an ellipsis goes),
    so the SPA can render "1 2 3 … N" without guessing how URLs are built.
    """
    pages = max(1, (max(0, total) + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    return {
        "page": page,
        "pages": pages,
        "total": max(0, total),
        "per_page": per_page,
        "prev": _page_link(base, page - 1) if page > 1 else None,
        "next": _page_link(base, page + 1) if page < pages else None,
        "links": _page_numbers(page, pages),
    }


def _slice_page(items, page, per_page=ROWS):
    """Return ``(window, page, pages, total)`` for an already-loaded list."""
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return items[start:start + per_page], page, pages, total


class ArchivePageError(Exception):
    """Archive returned an empty page even though it reported a non-zero total."""


def _archive_search_page(query, page, rows=ROWS):
    """Fetch one Archive page and clamp it against the real page count.

    Archive's advancedsearch endpoint returns ``numFound: 0`` and no docs for a
    far-out-of-range page, which used to render Every Pack as a genuinely empty
    room (``page=99999`` -> total 0).  When that happens we learn the total from
    page 1, clamp to the last page and fetch it.  An empty final page with a
    known non-zero total is an upstream flake: retry once, then fail visibly
    instead of pretending the room is empty.
    """
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError, OverflowError):
        page = 1
    docs, total = archive.search(query, page=page, rows=rows)
    if total > 0:
        known = total
        last = max(1, (known + rows - 1) // rows)
        if page > last:
            page = last
            docs, total = archive.search(query, page=page, rows=rows)
            total = total or known
    elif page > 1 and not docs:
        # Archive gave no total for the requested page; page 1 is authoritative.
        docs, first_total = archive.search(query, page=1, rows=rows)
        if first_total <= 0:
            return docs, 0, 1
        known = first_total
        last = max(1, (known + rows - 1) // rows)
        page = min(page, last)
        docs, total = archive.search(query, page=page, rows=rows)
        total = total or known
    if total > 0 and not docs:
        # One bounded retry for a transient empty final page.
        docs, retried_total = archive.search(query, page=page, rows=rows)
        total = retried_total or total
        if not docs:
            raise ArchivePageError(query)
    return docs, total, page


@app.errorhandler(ArchivePageError)
def _archive_page_error(exc):
    app.logger.warning("archive returned an empty page with a non-zero total: %s", exc)
    return _json_error("the archive search is temporarily unavailable", 502,
                       error_code="source_unavailable")


def _paged_payload(title, items, page, total, base, **extra):
    """Build a list payload with the pager and the legacy ``next`` link."""
    pager = _pager(page, total, base)
    payload = {"title": title, "items": items, "total": pager["total"],
               "pager": pager}
    payload.update(extra)
    if pager["next"]:
        payload["next"] = pager["next"]
    return payload


def _cursor_pager(page, base, has_next, has_prev=None, per_page=ROWS):
    """Pager for a server-side list whose total is not known up front.

    RaveTapePacks WordPress pagination only tells us whether another page
    exists, so the SPA shows "Page N" plus Previous/Next rather than inventing
    an M.  ``links`` contains just the current page (no numbered sequence).
    """
    if has_prev is None:
        has_prev = page > 1
    page = max(1, int(page))
    return {
        "page": page,
        "pages": None,
        "total": None,
        "per_page": per_page,
        "prev": _page_link(base, page - 1) if has_prev else None,
        "next": _page_link(base, page + 1) if has_next else None,
        "links": [page],
    }


def _optional_slice(items, base):
    """Apply ``?limit=&offset=`` to a complete source list.

    The default stays the complete list (LIST-CAPS.md): a client that wants a
    smaller first response opts in explicitly, and gets a ``next`` link.  The
    legacy indexes are bounded by the site nav (hundreds of rows), so no
    implicit cap is imposed.
    """
    raw_limit = request.args.get("limit")
    if raw_limit in (None, ""):
        return items, None
    try:
        limit = max(1, min(int(raw_limit), 500))
    except (TypeError, ValueError):
        return items, None
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    page = items[offset:offset + limit]
    nxt = None
    if offset + limit < len(items):
        separator = "&" if "?" in base else "?"
        nxt = "%s%slimit=%d&offset=%d" % (base, separator, limit, offset + limit)
    return page, nxt


# Independent provider calls (archive widen, SoundCloud, Mixcloud) run together
# so a cold /api/search costs the slowest, not their sum.
SEARCH_WORKERS = 3


def _gather(calls):
    """Run zero-argument callables concurrently; failures come back as None.

    Kept tiny on purpose: the caller knows which results it needs and merges
    them in the old order, so output stays identical to the sequential code.
    """
    if not calls:
        return []
    results = [None] * len(calls)
    if len(calls) == 1:
        try:
            results[0] = calls[0]()
        except Exception:
            results[0] = None
        return results
    try:
        with ThreadPoolExecutor(max_workers=min(SEARCH_WORKERS, len(calls))) as pool:
            futures = {pool.submit(call): index for index, call in enumerate(calls)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception:
                    results[index] = None
    except RuntimeError:  # pragma: no cover - thread pool unavailable
        for index, call in enumerate(calls):
            try:
                results[index] = call()
            except Exception:
                results[index] = None
    return results


def _pack_items(docs):
    """Turn Archive search docs into folder rows, skipping malformed entries."""
    items = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        identifier = doc.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            continue
        title = _clean_text(doc.get("title"), MAX_TITLE) or identifier
        items.append(_folder(title, _year_label(doc),
                             "/api/item/%s" % urllib.parse.quote(identifier),
                             archive.thumb(identifier),
                             {"kind": "pack", "target": identifier, "title": title}))
    return items


def _json_error(message, code, headers=None, error_code=None):
    payload = {"error": message}
    if error_code:
        payload["code"] = error_code
    response = jsonify(payload)
    response.status_code = code
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


# --------------------------------------------------------------------------- auth guard + login


def _request_is_https():
    if request.is_secure:
        return True
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0]
    return forwarded.strip().lower() == "https"


def _client_ip():
    return (request.remote_addr or "unknown")[:64]


def _safe_next(value):
    """Only same-site absolute paths are allowed as post-login targets."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value.startswith("/") or value.startswith("//"):
        return ""
    if "\\" in value or "\r" in value or "\n" in value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme or parts.netloc:
        return ""
    return value


def _session_valid():
    if not session.get("auth"):
        return False
    if session.get("identity") not in ("owner", "friends"):
        session.clear()
        return False
    if session.get("role") not in ("admin", "viewer"):
        session.clear()
        return False
    sid = session.get("sid")
    try:
        if not sid:
            session.clear()
            return False
        # One connection for the session lookup, the block check and the
        # throttled last_seen write (was three connections + a write per API
        # request, which showed up on every hub navigation).  Deliberately not
        # memoised: a revoke or a block must deny the very next request.
        active, blocked = audit.session_check(
            sid, session.get("identity") or "", _client_ip(),
            (request.headers.get("User-Agent") or "")[:300])
        if not active or blocked:
            session.clear()
            return False
        g._audit_touched = True
    except Exception:  # an unreadable audit store fails closed
        app.logger.exception("auth: session store unavailable")
        session.clear()
        return False
    started = session.get("login_at")
    try:
        started = float(started)
    except (TypeError, ValueError):
        session.clear()
        return False
    max_age = ((AUTH.remember_days if session.get("remember") else AUTH.session_hours)
               * 3600.0)
    if max_age <= 0 or time.time() - started > max_age:
        session.clear()
        return False
    return True


def _csrf_token():
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _widget_token_expected():
    return (os.environ.get("RAVEWEB_WIDGET_TOKEN") or "").strip()


def _widget_authorized():
    expected = _widget_token_expected()
    if not expected:
        return False
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return False
    return auth.same_text(header[7:].strip(), expected)


PUBLIC_PATHS = frozenset((
    "/login", "/logout", "/healthz", "/manifest.webmanifest",
    "/manifest.json", "/sw.js",
    "/static/auth.css", "/static/login.js", "/static/sw-register.js",
    "/api/csp-report",
    "/static/art/icon.png", "/static/art/icon-180.png",
))


def _public_static(path):
    return (path in PUBLIC_PATHS
            or path.startswith("/static/art/icon") and path.endswith(".png"))


# --------------------------------------------------------------------------- request ids
# 1.6.31: every response carries X-Request-ID and every access log line quotes
# it, so an event in the client debug log can be matched to the server line.
# Incoming values are validated (log-injection safe) and never reflected raw.

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def _clean_request_id(value):
    value = (value or "").strip()
    return value if _REQUEST_ID_RE.match(value) else ""


def _current_request_id():
    try:
        return getattr(g, "request_id", "") or "-"
    except RuntimeError:  # background thread, no request context
        return "-"


@app.before_request
def _assign_request_id():
    g.request_id = (_clean_request_id(request.headers.get("X-Request-ID"))
                    or secrets.token_hex(8))
    g.request_started = time.perf_counter()


@app.before_request
def _auth_guard():
    """Protect the shell, every API route and non-login static assets."""
    path = request.path
    if path.startswith("/api/"):
        if path == "/api/csp-report":
            return None
        if path == "/api/admin/stats" and _widget_authorized():
            return None
        # A media player (Kodi, VLC, a TV) cannot present the browser session
        # cookie.  /api/mega/stream accepts a short-lived HMAC token minted by
        # /api/mega/sign; the route validates it again and fails closed.
        if path == "/api/mega/stream" and mega_stream_has_valid_token(request.args):
            return None
        if not _session_valid():
            return _json_error("authentication required", 401)
        return None
    if path.startswith("/static/"):
        if _session_valid() or _public_static(path) or path in PUBLIC_PATHS:
            return None
        return Response("Unauthorized", status=401, mimetype="text/plain")
    if path in PUBLIC_PATHS:
        return None
    if _session_valid():
        return None
    target = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
    return redirect(url_for("login_page", next=_safe_next(target) or "/"))


def _render_login(next_url="", error="", status=200, retry_after=None):
    headers = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = str(int(retry_after))
    return render_template(
        "login.html",
        next_url=_safe_next(next_url) or "",
        csrf=_csrf_token(),
        error=error,
        version=APP_VERSION,
    ), status, headers


@app.get("/login")
def login_page():
    if _session_valid():
        return redirect(_safe_next(request.args.get("next")) or "/")
    return _render_login(request.args.get("next", ""))


@app.post("/login")
def login_submit():
    next_url = _safe_next(request.form.get("next"))
    ip = _client_ip()
    submitted = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not submitted or not expected or not auth.same_text(submitted, expected):
        app.logger.warning("auth: login csrf rejected ip=%s", ip)
        audit.record_event("login_csrf_rejected", ip=ip,
                           user_agent=request.headers.get("User-Agent", ""),
                           endpoint="/login", status=400)
        return _render_login(next_url, "Your sign-in session expired — try again.", 400)
    identity_attempt = (request.form.get("username") or "").strip().lower()[:64]
    try:
        if audit.is_blocked(identity_attempt, ip):
            audit.record_event("login_blocked", identity=identity_attempt, ip=ip,
                               user_agent=request.headers.get("User-Agent", ""),
                               endpoint="/login", status=403)
            return _render_login(next_url, "Access temporarily blocked.", 403)
    except Exception:
        app.logger.exception("auth: block check failed")
        return _render_login(next_url, "Sign-in is temporarily unavailable.", 503)
    lock_key = "%s\x00%s" % (ip, identity_attempt or "-")
    retry = LOGIN_LIMITER.retry_after(lock_key)
    if retry:
        app.logger.warning("auth: login locked ip=%s identity=%s retry=%ss",
                           ip, lock_key.split("\x00", 1)[-1], retry)
        audit.record_event("login_locked", identity=lock_key.split("\x00", 1)[-1],
                           ip=ip, user_agent=request.headers.get("User-Agent", ""),
                           endpoint="/login", status=429)
        return _render_login(
            next_url, "Too many attempts. Try again in %d seconds." % retry,
            429, retry_after=retry)
    identity = identity_attempt
    password = request.form.get("password") or ""
    # Fixed single-operator identities: owner=admin, friends=viewer.  Missing
    # friends hash disables that identity without revealing it on the page.
    credentials = {"owner": ("admin", AUTH.password_hash)}
    try:
        friends_disabled = audit.get_setting("friends_disabled") == "1"
    except Exception:
        friends_disabled = True  # fail closed for the viewer identity
    if AUTH.friends_hash and not friends_disabled:
        credentials["friends"] = ("viewer", AUTH.friends_hash)
    entry = credentials.get(identity)
    role = entry[0] if entry else ""
    # Unknown usernames still run a hash check against the owner hash so timing
    # does not reveal which identities exist.
    reference_hash = entry[1] if entry else AUTH.password_hash
    password_ok = auth.verify_password(reference_hash, password)
    lock_key = "%s\x00%s" % (ip, identity or "-")
    if entry and password_ok:
        LOGIN_LIMITER.reset(lock_key)
        remember = bool(request.form.get("remember"))
        ttl = ((AUTH.remember_days if remember else AUTH.session_hours) * 3600.0)
        sid = audit.create_session(identity, role, ip,
                                   request.headers.get("User-Agent", ""),
                                   ttl_seconds=ttl, remember=remember)
        session.clear()
        session["auth"] = True
        session["identity"] = identity
        session["user"] = identity
        session["role"] = role
        session["sid"] = sid
        session["login_at"] = time.time()
        session["remember"] = remember
        session.permanent = remember
        session["csrf"] = secrets.token_urlsafe(32)
        audit.record_event("login_success", identity=identity, role=role, ip=ip,
                           user_agent=request.headers.get("User-Agent", ""),
                           endpoint="/login", status=302)
        app.logger.info("auth: login success ip=%s identity=%s role=%s",
                        ip, identity, role)
        return redirect(next_url or "/")
    LOGIN_LIMITER.fail(lock_key)
    audit.record_event("login_failure", identity=identity, role=role, ip=ip,
                       user_agent=request.headers.get("User-Agent", ""),
                       endpoint="/login", status=401)
    app.logger.warning("auth: login failure ip=%s identity=%s role=%s",
                       ip, identity[:64] or "-", role or "-")
    return _render_login(next_url, "Invalid username or password.", 401)


def require_admin(view):
    """Reusable decorator: only the owner (admin role) may pass.

    APIs get a 403 JSON response; page routes get a plain 403.  Put it on any
    future admin-only route; the global auth guard still runs first.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _session_valid():
            if request.path.startswith("/api/"):
                return _json_error("authentication required", 401)
            return redirect(url_for("login_page",
                                    next=_safe_next(request.path) or "/"))
        if session.get("role") != "admin":
            return _json_error("admin access required", 403)
        return view(*args, **kwargs)
    return wrapped


@app.get("/api/admin/whoami")
@require_admin
def admin_whoami():
    return jsonify({"ok": True, "identity": session.get("identity"),
                    "role": session.get("role")})


@app.post("/api/playback")
def api_playback_heartbeat():
    """Authenticated, best-effort now-playing heartbeat; no URLs are stored."""
    if request.content_length and request.content_length > 4096:
        return _json_error("heartbeat is too large", 413)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_error("expected a JSON object", 400)
    state = _clean_text(payload.get("state"), 12).lower()
    if state not in playback.STATES:
        return _json_error("state must be playing, paused or stopped", 400)
    raw_label = payload.get("label", "")
    raw_source = payload.get("source", "")
    if not isinstance(raw_label, str) or len(raw_label) > 300:
        return _json_error("label must be a string up to 300 characters", 400)
    if not isinstance(raw_source, str) or len(raw_source) > 64:
        return _json_error("source must be a string up to 64 characters", 400)
    if "://" in raw_label or "://" in raw_source:
        return _json_error("playback metadata cannot contain URLs", 400)
    label = _clean_text(raw_label, 300)
    source = _clean_text(raw_source, 64)
    try:
        position = float(payload.get("position") or 0)
        duration_raw = payload.get("duration")
        duration = None if duration_raw in (None, "") else float(duration_raw)
        client_ts = payload.get("client_ts")
        client_ts = None if client_ts in (None, "") else float(client_ts)
    except (TypeError, ValueError, OverflowError):
        return _json_error("position, duration and client_ts must be numbers", 400)
    if (not math.isfinite(position) or position < 0 or position > 86400
            or (duration is not None and (not math.isfinite(duration)
                                          or duration < 0 or duration > 86400))):
        return _json_error("playback position/duration are out of range", 400)
    sid = session.get("sid")
    identity = session.get("identity") or ""
    role = session.get("role") or ""
    if not sid or not identity:
        return _json_error("authentication required", 401)
    key = "playback:%s" % sid
    now = time.monotonic()
    with _PLAYBACK_RATE["lock"]:
        recent = [stamp for stamp in _PLAYBACK_RATE["state"].get(key, [])
                  if stamp > now - 60]
        if len(recent) >= PLAYBACK_RATE_LIMIT:
            _PLAYBACK_RATE["state"][key] = recent
            return _json_error("playback heartbeats are too frequent", 429)
        recent.append(now)
        _PLAYBACK_RATE["state"][key] = recent
    try:
        playback.record(identity, sid, role, state, label, source,
                        position, duration, client_ts)
    except Exception:
        app.logger.exception("playback heartbeat failed")
        # Best effort: never make the player deal with a tracking failure.
    return Response("", status=204)


@app.get("/api/admin/playback")
@require_admin
def admin_playback():
    return jsonify({"now": playback.now_playing(),
                    "stale_after_seconds": playback.STALE_AFTER})


@app.get("/api/admin/playback/history")
@require_admin
def admin_playback_history():
    try:
        hours = float(request.args.get("hours", "24"))
    except (TypeError, ValueError):
        return _json_error("hours must be a number", 400)
    if hours < 1 or hours > 168:
        return _json_error("hours must be between 1 and 168", 400)
    identity = (request.args.get("identity") or "").strip().lower()
    if identity and identity not in ("owner", "friends"):
        return _json_error("unknown identity", 400)
    return jsonify({"hours": hours, "identity": identity or None,
                    "items": playback.history(hours, identity or None)})


@app.get("/api/admin/playback/export")
@require_admin
def admin_playback_export():
    try:
        hours = float(request.args.get("hours", "24"))
    except (TypeError, ValueError):
        return _json_error("hours must be a number", 400)
    if hours < 1 or hours > 168:
        return _json_error("hours must be between 1 and 168", 400)
    payload = playback.export_payload(hours)
    response = jsonify(payload)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    response.headers["Content-Disposition"] = (
        'attachment; filename="rave-playback-%s.json"' % stamp)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/admin/stats")
def admin_stats():
    """Read-only Control Centre feed: owner session or widget bearer token."""
    if not _widget_authorized():
        if not _session_valid():
            return _json_error("authentication required", 401)
        if session.get("role") != "admin":
            return _json_error("admin access required", 403)
    try:
        payload = audit.stats()
    except Exception:
        app.logger.exception("audit: stats unavailable")
        return jsonify({"status": "down", "generated_at": None,
                        "error": "stats unavailable"}), 503
    return jsonify(payload)


# --------------------------------------------------------------------------- admin write API


def _audit_admin(event, detail="", status=200):
    try:
        audit.record_event(event, identity=session.get("identity") or "",
                           role=session.get("role") or "", ip=_client_ip(),
                           user_agent=request.headers.get("User-Agent", ""),
                           endpoint=request.path, status=status,
                           detail=detail)
    except Exception:
        app.logger.exception("audit: admin action record failed")


def _admin_csrf_ok():
    submitted = (request.headers.get("X-CSRF-Token")
                 or request.form.get("csrf") or "")
    if not submitted and request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            submitted = payload.get("csrf") or ""
    return bool(submitted) and auth.same_text(submitted, session.get("csrf", ""))


def _admin_mutation_error():
    if not _admin_csrf_ok():
        _audit_admin("admin_csrf_rejected", "", 403)
        return _json_error("csrf token required", 403)
    key = "admin:%s" % _client_ip()
    try:
        retry = ADMIN_ACTION_LIMITER.retry_after(key)
        if retry:
            _audit_admin("admin_rate_limited", "", 429)
            return _json_error("too many admin actions", 429,
                               {"Retry-After": str(retry)})
        ADMIN_ACTION_LIMITER.fail(key)
    except Exception:
        app.logger.exception("admin: rate limit store unavailable")
        return _json_error("admin actions temporarily unavailable", 503)
    return None


def _block_public(block):
    kind = "identity" if block.get("identity") else "ip"
    return {
        "id": block["id"],
        "kind": kind,
        "value": block.get("identity") if kind == "identity" else block.get("ip"),
        "reason": block.get("reason") or "",
        "created_at": block.get("created_at"),
        "until": block.get("until"),
        "created_by": block.get("created_by") or "",
    }


@app.get("/api/admin/csrf")
@require_admin
def admin_csrf():
    return jsonify({"csrf": _csrf_token()})


@app.get("/api/admin/sessions")
@require_admin
def admin_sessions():
    items = [{
        "session_id_masked": audit.masked_session_id(row["id"]),
        "identity": row["identity"], "role": row["role"], "ip": row["ip"],
        "created": row["created_at"], "last_seen": row["last_seen"],
        "expires_at": row["expires_at"],
    } for row in audit.list_sessions()]
    return jsonify({"sessions": items})


@app.post("/api/admin/sessions/<masked_id>/revoke")
@require_admin
def admin_revoke_session(masked_id):
    err = _admin_mutation_error()
    if err:
        return err
    row = audit.find_session_by_masked(masked_id)
    if not row:
        _audit_admin("admin_session_revoke_failed", masked_id, 404)
        return _json_error("unknown session", 404)
    audit.revoke_session(row["id"])
    _audit_admin("admin_session_revoked", masked_id, 200)
    return jsonify({"revoked": True, "session_id_masked": masked_id,
                    "identity": row["identity"], "role": row["role"]})


@app.get("/api/admin/blocks")
@require_admin
def admin_blocks():
    return jsonify({"blocks": [_block_public(row) for row in audit.list_blocks()]})


@app.post("/api/admin/blocks")
@require_admin
def admin_block_create():
    err = _admin_mutation_error()
    if err:
        return err
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_error("expected a JSON object", 400)
    kind = _clean_text(payload.get("kind"), 16).lower()
    value = _clean_text(payload.get("value"), 64).strip()
    reason = _clean_text(payload.get("reason"), 300).strip()
    if kind not in ("ip", "identity") or not value:
        return _json_error("kind must be ip or identity, with a value", 400)
    try:
        ttl = int(payload.get("ttl_seconds", payload.get("ttl", 3600)) or 3600)
    except (TypeError, ValueError):
        return _json_error("ttl_seconds must be an integer", 400)
    ttl = max(60, min(ttl, 86400 * 30))
    if kind == "ip":
        if not audit.valid_ip(value):
            return _json_error("invalid ip address", 400)
        until = audit.add_block(ip=value, seconds=ttl, reason=reason,
                                created_by=session.get("identity") or "owner")
        audit.revoke_sessions(ip=value)
    else:
        value = value.lower()
        until = audit.add_block(identity=value, seconds=ttl, reason=reason,
                                created_by=session.get("identity") or "owner")
        audit.revoke_sessions(identity=value)
    _audit_admin("admin_block_created", "%s:%s" % (kind, value), 201)
    blocks = audit.list_blocks()
    created = next((row for row in blocks if row["until"] == until
                    and ((row["identity"] == value) if kind == "identity"
                         else (row["ip"] == value))), None)
    return jsonify({"block": _block_public(created) if created else None}), 201


@app.delete("/api/admin/blocks/<int:block_id>")
@require_admin
def admin_block_delete(block_id):
    err = _admin_mutation_error()
    if err:
        return err
    block = audit.get_block(block_id)
    if not block:
        return _json_error("unknown block", 404)
    audit.delete_block(block_id)
    _audit_admin("admin_block_deleted", "%s:%s" % (
        "identity" if block.get("identity") else "ip",
        block.get("identity") or block.get("ip")), 200)
    return jsonify({"deleted": True, "id": block_id})


@app.get("/api/admin/friends")
@require_admin
def admin_friends_status():
    try:
        disabled = audit.get_setting("friends_disabled") == "1"
    except Exception:
        return _json_error("settings unavailable", 503)
    return jsonify({"friends_enabled": bool(AUTH.friends_hash) and not disabled,
                    "disabled": disabled})


@app.post("/api/admin/friends/disable")
@require_admin
def admin_friends_disable():
    err = _admin_mutation_error()
    if err:
        return err
    audit.set_setting("friends_disabled", "1")
    audit.revoke_sessions(identity="friends")
    _audit_admin("admin_friends_disabled", "", 200)
    return jsonify({"friends_enabled": False})


@app.post("/api/admin/friends/enable")
@require_admin
def admin_friends_enable():
    err = _admin_mutation_error()
    if err:
        return err
    audit.delete_setting("friends_disabled")
    _audit_admin("admin_friends_enabled", "", 200)
    enabled = bool(AUTH.friends_hash)
    return jsonify({"friends_enabled": enabled})


@app.after_request
def _record_request_audit(response):
    """Persist endpoint/bytes/identity for the stats feed (best-effort)."""
    try:
        path = request.path
        if (path.startswith("/static/")
                or path in ("/healthz", "/manifest.webmanifest",
                            "/manifest.json", "/sw.js")):
            return response
        if not (path.startswith("/api/") or path in ("/", "/login")
                or path.startswith("/downloads")):
            return response
        role = session.get("role") if session.get("sid") else ""
        identity = session.get("identity") if session.get("sid") else ""
        size = response.content_length or 0
        audit.record_event(
            "request", identity=identity or "", role=role or "",
            ip=_client_ip(), user_agent=request.headers.get("User-Agent", ""),
            endpoint=path, bytes=size, status=response.status_code)
    except Exception:  # auditing must never break a response
        app.logger.exception("audit: request record failed")
    return response


@app.post("/api/csp-report")
def csp_report():
    """Collect browser CSP violation reports (report-only phase)."""
    raw = request.get_data(cache=False, as_text=False)
    if len(raw) > 64 * 1024:
        return Response("", status=413, mimetype="text/plain")
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "null")
    except (ValueError, TypeError):
        return Response("", status=204)
    reports = payload if isinstance(payload, list) else [payload]
    for entry in reports[:20]:
        if not isinstance(entry, dict):
            continue
        report = entry.get("csp-report") if isinstance(entry.get("csp-report"), dict) else entry
        app.logger.warning(
            "csp violation directive=%s blocked=%s document=%s disposition=%s",
            str(report.get("effective-directive") or report.get("violated-directive") or "-")[:80],
            str(report.get("blocked-uri") or "-")[:300],
            str(report.get("document-uri") or "-")[:300],
            str(report.get("disposition") or "-")[:20])
    return Response("", status=204)


@app.post("/logout")
def logout():
    identity = session.get("identity") or ""
    role = session.get("role") or ""
    sid = session.get("sid")
    if sid:
        try:
            audit.revoke_session(sid)
        except Exception:  # logging out must never 500
            app.logger.exception("auth: could not revoke session on logout")
    try:
        audit.record_event("logout", identity=identity, role=role,
                           ip=_client_ip(),
                           user_agent=request.headers.get("User-Agent", ""),
                           endpoint="/logout", status=302)
    except Exception:
        app.logger.exception("auth: logout audit failed")
    session.clear()
    app.logger.info("auth: logout ip=%s identity=%s role=%s",
                    _client_ip(), identity or "-", role or "-")
    response = redirect(url_for("login_page"))
    response.delete_cookie(app.config["SESSION_COOKIE_NAME"], path="/")
    return response


# --------------------------------------------------------------------------- api: structure


def _rail_payload():
    """Rail groups with their resolved paths (and child paths for highlighting)."""
    rail = []
    for group in RAIL:
        path = group.get("path") or "/api/rail/%s" % group["id"]
        rail.append({
            "id": group["id"],
            "label": group["label"],
            "icon": ART + group["icon"],
            "sub": group.get("sub", ""),
            "path": path,
            "children": [{"id": sid, "path": SECTION_PATHS[sid]}
                         for sid in group.get("children", []) if sid in SECTION_PATHS],
        })
    return rail


@app.get("/api/menu")
def api_menu():
    return jsonify({"sections": [
        {"id": sid, "label": label, "icon": ART + icon, "sub": sub,
         "path": SECTION_PATHS.get(sid, "")}
        for sid, label, icon, sub in SECTIONS
    ], "rail": _rail_payload(),
       "site": {"title": "Rave Player", "version": APP_VERSION}})


@app.get("/api/rail/<key>")
def api_rail(key):
    """Hub screen for a grouped rail button: one folder per child section."""
    group = next((entry for entry in RAIL if entry["id"] == key), None)
    if not group:
        return _json_error("unknown rail group", 404)
    items = []
    for sid in group.get("children", []):
        section = next((entry for entry in SECTIONS if entry[0] == sid), None)
        if not section:
            continue
        items.append(_folder(section[1], section[3], SECTION_PATHS.get(sid, ""),
                             ART + section[2]))
    if not items:
        return _json_error("empty rail group", 404)
    return jsonify({"title": group["label"], "items": items})


def _surprise_candidates():
    """One random pack or show; the endpoint picks one of these."""
    candidates = []
    try:
        docs, _ = archive.search("collection:ravetapes AND mediatype:audio", rows=12)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            identifier = doc.get("identifier")
            if not identifier:
                continue
            title = _clean_text(doc.get("title"), MAX_TITLE) or identifier
            candidates.append({
                "label": title,
                "sub": "Random tape pack",
                "open": "/api/item/%s" % urllib.parse.quote(str(identifier)),
            })
    except Exception:  # noqa: BLE001 - one source down must not break surprise
        pass
    try:
        for entry in feeds.registry():
            feed_id = entry.get("id") if isinstance(entry, dict) else None
            if not feed_id:
                continue
            candidates.append({
                "label": _clean_text(entry.get("name"), MAX_TITLE) or str(feed_id),
                "sub": "Random show",
                "open": "/api/podcast/%s" % urllib.parse.quote(str(feed_id)),
            })
    except Exception:  # noqa: BLE001
        pass
    return candidates


@app.get("/api/surprise")
def api_surprise():
    """Surprise Me: one random tape pack or show to open."""
    candidates = _surprise_candidates()
    if not candidates:
        return _json_error("could not find a surprise right now", 503)
    return jsonify(random.choice(candidates))


@app.get("/api/latest")
def api_latest():
    try:
        entries = latest.merge(limit=60)
    except Exception as exc:  # one dead source must not take the page down
        app.logger.warning("latest merge failed: %s", exc)
        return _json_error("could not load latest", 502)
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        when = entry.get("when")
        stamp = when.strftime("%d %b %Y") if when else ""
        kind = entry.get("kind")
        title = _clean_text(entry.get("title"), MAX_TITLE)
        if not title:
            continue
        if kind == "pack" and entry.get("identifier"):
            identifier = str(entry["identifier"])
            items.append(_folder(title, "%s · %s" % (entry.get("source", ""), stamp),
                                 "/api/item/%s" % urllib.parse.quote(identifier),
                                 archive.thumb(identifier),
                                 {"kind": "pack", "target": identifier, "title": title}))
        elif kind == "episode" and entry.get("url"):
            url = _podomatic_url(str(entry["url"]))
            item = _track(title, "%s · %s" % (entry.get("source", ""), stamp), url,
                          ART + "podcasts.png",
                          {"kind": "stream", "target": url, "title": title})
            if _is_podomatic_url(url):
                _mark_download(item, "podomatic", url)
            items.append(item)
        elif kind == "dnb" and entry.get("id") is not None:
            item = _track(title, "%s · %s" % (entry.get("source", ""), stamp), None,
                          ART + "dnb.png",
                          {"kind": "dnb", "target": str(entry["id"]), "title": title})
            item["play_via"] = "/api/dnb/%s" % urllib.parse.quote(str(entry["id"]))
            items.append(item)
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("Latest", window, page, total, "/api/latest"))


@app.get("/api/filters")
def api_filters():
    return jsonify({"title": "Quick Filters", "items": [
        _folder(entry["name"], entry.get("plot", ""),
                "/api/filter/%s" % urllib.parse.quote(entry["id"]),
                ART + FILTER_ART.get(entry["id"], "series.png"))
        for entry in _filters()
    ]})


@app.get("/api/filter/<key>")
def api_filter(key):
    entry = _filter(key)
    if not entry:
        return _json_error("unknown filter", 404)
    page = _page_arg()
    items = []
    if page == 1 and entry.get("live_name"):
        item = _track("▶ %s — live now" % entry["name"], "Live streams", None,
                      ART + ("kool.png" if entry["id"] == "kool" else "rinse.png"))
        item["play_via"] = "/api/filter/%s/live" % urllib.parse.quote(key)
        items.append(item)
    docs, total, page = _archive_search_page(entry["query"], page, ROWS)
    items.extend(_pack_items(docs))
    return jsonify(_paged_payload(entry["name"], items, page, total,
                                  "/api/filter/%s" % key))


@app.get("/api/filter/<key>/live")
def api_filter_live(key):
    entry = _filter(key)
    if not entry:
        return _json_error("unknown filter", 404)
    stations = radio.curated(entry.get("live_name", ""))
    if not stations:
        token = str(entry.get("live_name", "")).lower()
        stations = [station for station in radio.stations_by_name(entry.get("live_name", ""), limit=20)
                    if token and token in str(station.get("name", "")).lower()]
    items = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        name, meta = radio.display(station)
        url = radio.stream_url(station)
        if not url:
            continue
        items.append(_track(name, meta, url,
                            station.get("favicon") or ART + "radio.png",
                            {"kind": "stream", "target": url, "title": name}))
    return jsonify({"title": "%s — live" % entry["name"], "items": items})


@app.get("/api/live")
def api_live():
    """Live Radio: named stations first, then the scene genres."""
    items = []
    for token, label, icon in (("kool", "Kool FM", "kool.png"),
                               ("rinse", "Rinse FM", "rinse.png"),
                               ("centreforce", "Centreforce 883", "centreforce.png"),
                               ("dejavu", "DejaVu FM", "dejavu.png"),
                               ("osa", "OSA Radio", "osa.png")):
        station = next((entry for entry in radio.curated(token) if entry), None)
        if station:
            url = radio.stream_url(station)
            items.append(_track(label, "Live now", url, ART + icon,
                                {"kind": "stream", "target": url, "title": label}))
    for key, label, _tag in RADIO_SECTIONS:
        items.append(_folder(label, "Stations on air", "/api/live/%s" % key,
                             ART + LIVE_ART[key]))
    return jsonify({"title": "Live Radio", "items": items})


@app.get("/api/live/<key>")
def api_live_tag(key):
    section = next((entry for entry in RADIO_SECTIONS if entry[0] == key), None)
    if not section:
        return _json_error("unknown station group", 404)
    stations = radio.stations(section[2], limit=500)
    valid = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        name, meta = radio.display(station)
        url = radio.stream_url(station)
        if url:
            valid.append((station, name, meta, url))
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    items = [
        _track(name, meta, url, station.get("favicon") or ART + "radio.png",
               {"kind": "stream", "target": url, "title": name})
        for station, name, meta, url in window]

    return jsonify(_paged_payload(section[1], items, page, total,
                                  "/api/live/%s" % key))


@app.get("/api/mixcloud")
def api_mixcloud():
    query = request.args.get("q", "DJ EZ").strip()
    limit = request.args.get("limit", type=int) or 24
    # One search call: two calls could see different cache states and pair a
    # title with another set's embed URL (audit risk 3).
    entries = mixcloud.search(query, limit=limit)
    items = []
    for entry in entries:
        item = _track(entry["title"],
                      "%s%s" % (entry["user"],
                                (" · " + entry["created"][:10]) if entry.get("created") else ""),
                      None, entry.get("thumb") or ART + "mixcloud.png")
        item["embed"] = entry["embed"]
        item["external"] = entry["url"]
        items.append(item)
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("Mixcloud: %s" % query, window, page, total,
                                  "/api/mixcloud?q=%s" % urllib.parse.quote(query)))


@app.get("/api/uk-garage")
def api_uk_garage():
    """UK garage hub: Mixcloud DJ EZ sets (embeds) plus Archive garage packs."""
    items = []
    for entry in mixcloud.search("DJ EZ", limit=18):
        item = _track(entry["title"], "Mixcloud · %s" % entry["user"], None,
                      entry.get("thumb") or ART + "garage.png")
        item["embed"] = entry["embed"]
        item["external"] = entry["url"]
        items.append(item)
    garage_query = next((entry[2] for entry in ARCHIVE_SECTIONS if entry[0] == "garage"),
                        "collection:ravetapes AND house")
    docs, _ = archive.search(garage_query, rows=30)
    items.extend(_pack_items(docs))
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("UK Garage", window, page, total,
                                  "/api/uk-garage"))


@app.get("/api/ukg")
def api_ukg_legacy():
    """Pre-1.6.43 spelling: keep old bookmarks working, but the canonical
    route (and every app link) is now /api/uk-garage."""
    return redirect("/api/uk-garage", code=301)


MIXCLOUD_QUERIES = [
    # discovery
    ("Live sets", "live set", "Sets recorded live in the room"),
    ("Jungle live", "jungle live", "Live jungle sets"),
    ("Drum & bass live", "drum and bass live", "Live DnB sets"),
    ("Rave / club", "rave", "Rave and club sets"),
    ("Kool FM", "kool fm", "Kool FM shows and sets"),
    # oldskool rave and jungle promotions
    ("Jungle Fever", "jungle fever", "Jungle Fever sets"),
    ("Desire", "desire rave", "Desire rave sets"),
    ("Roast", "roast rave", "Roast sets"),
    ("Telepathy", "telepathy jungle", "Telepathy jungle sets"),
    ("AWOL", "awol jungle", "AWOL jungle sets"),
    ("World Dance", "world dance", "World Dance sets"),
    ("Raindance", "raindance rave", "Raindance sets"),
    ("One Nation", "one nation", "One Nation sets"),
    ("Fantazia", "fantazia", "Fantazia sets"),
    ("Helter Skelter", "helter skelter", "Helter Skelter sets"),
    ("Dreamscape", "dreamscape", "Dreamscape sets"),
    ("Amnesia House", "amnesia house", "Amnesia House sets"),
    ("Rezerection", "rezerection", "Rezerection sets"),
    ("Slammin' Vinyl", "slammin vinyl", "Slammin' Vinyl sets"),
    ("Vibealite", "vibealite", "Vibealite sets"),
    ("Hardcore Heaven", "hardcore heaven", "Hardcore Heaven sets"),
    ("Accelerated Culture", "accelerated culture", "Accelerated Culture sets"),
    ("Seduction", "seduction rave", "Seduction sets"),
    ("Elevation", "elevation rave", "Elevation sets"),
    ("Sun City", "sun city", "Sun City sets"),
    # garage / house
    ("Garage Nation", "garage nation", "Garage Nation live recordings"),
    ("fabric London", "fabric london", "fabric sets"),
    ("Ministry of Sound", "ministry of sound", "Ministry sets and mixes"),
]


@app.get("/api/mixcloud_hub")
def api_mixcloud_hub():
    return jsonify({"title": "Mixcloud Sets", "items": [
        _folder(label, sub, "/api/mixcloud?q=%s" % urllib.parse.quote(query),
                ART + "mixcloud.png") for label, query, sub in MIXCLOUD_QUERIES
    ]})


@app.get("/api/soundcloud")
def api_soundcloud():
    query = request.args.get("q", "").strip()
    if query and soundcloud.configured():
        items = []
        for entry in soundcloud.search(query):
            item = _track(entry["title"], "SoundCloud · %s" % entry["user"], None,
                          entry.get("thumb") or ART + "soundcloud.png")
            item["embed"] = entry["embed"]
            item["external"] = entry["url"]
            items.append(item)
        return jsonify({"title": "SoundCloud: %s" % query, "items": items})
    items = []
    for entry in soundcloud.registry():
        item = _track(entry["title"], "SoundCloud · live set", None, ART + "soundcloud.png")
        item["embed"] = soundcloud.embed_url(entry["url"])
        item["external"] = entry["url"]
        items.append(item)
    return jsonify({"title": "SoundCloud Sets", "items": items})


@app.get("/api/raves")
def api_raves():
    items = []
    for entry in raves.registry():
        bits = []
        if entry["archive_count"]:
            bits.append("%d packs" % entry["archive_count"])
        if entry["mixcloud_count"]:
            bits.append("%d sets" % entry["mixcloud_count"])
        items.append(_folder(entry["name"], " · ".join(bits) or "content",
                             "/api/rave/%s" % entry["slug"], ART + "rave-nights.png"))
    items.sort(key=lambda item: item["label"].lower())
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("Rave Nights", window, page, total,
                                  "/api/raves"))


@app.get("/api/rave/<key>")
def api_rave(key):
    entry = raves.entry(key)
    if not entry:
        return jsonify({"error": "unknown rave"}), 404
    items = []
    if entry["mixcloud_query"] and entry["mixcloud_count"]:
        for set_entry in mixcloud.search(entry["mixcloud_query"], limit=12):
            item = _track(set_entry["title"], "Mixcloud · %s" % set_entry["user"], None,
                          set_entry.get("thumb") or ART + "mixcloud.png")
            item["embed"] = set_entry["embed"]
            item["external"] = set_entry["url"]
            items.append(item)
    if entry["archive_query"] and entry["archive_count"]:
        docs, _ = archive.search(entry["archive_query"], rows=30)
        items.extend(_pack_items(docs))
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload(entry["name"], window, page, total,
                                  "/api/rave/%s" % urllib.parse.quote(str(key))))


# --------------------------------------------------------------------------- 1.6.14 mirror sources


@app.get("/api/moxser")
def api_moxser():
    """Moxser mixes (sitemap -> page -> direct MP3, resolved on play)."""
    try:
        entries = moxser.mixes()
    except Exception as exc:
        app.logger.warning("moxser list failed: %s", exc)
        return _json_error("could not load Moxser", 502)
    valid = []
    for entry in entries:
        title = _clean_text(entry.get("title"), MAX_TITLE)
        slug = entry.get("slug") or ""
        if title and slug:
            valid.append((entry, title, slug))
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    items = []
    for entry, title, slug in window:
        item = _track(title, _source_sub("Moxser", entry.get("date")), None,
                      _http_url(entry.get("flyer")) or ART + "moxser.png")
        item["play_via"] = "/api/moxser/%s" % urllib.parse.quote(slug, safe="")
        item["download"] = {"source": "moxser"}
        items.append(item)
    return jsonify(_paged_payload("Moxser Mixes", items, page, total,
                                  "/api/moxser"))


@app.get("/api/moxser/<path:slug>")
def api_moxser_resolve(slug):
    mix = moxser.detail(slug)
    if not mix:
        return _json_error("could not resolve that mix", 404)
    url = _http_url(mix.get("url"))
    if not url:
        return _json_error("could not resolve that mix", 404)
    return jsonify({
        "url": url,
        "title": _clean_text(mix.get("title"), MAX_TITLE),
        "sub": _source_sub("Moxser", mix.get("date")),
        "art": _http_url(mix.get("flyer")) or ART + "moxser.png",
    })


@app.get("/api/hardscore")
def api_hardscore():
    """Hardscore mixes with direct audio, plus their event listings."""
    items = []
    for entry in hardscore.mixes():
        title = _clean_text(entry.get("title"), MAX_TITLE)
        if not title:
            continue
        sub = _source_sub("Hardscore", entry.get("date"))
        media = _http_url(entry.get("media"))
        if media:
            item = _track(title, sub, media, ART + "hardscore.png",
                          {"kind": "stream", "target": media, "title": title})
            _mark_download(item, "hardscore", media)
            items.append(item)
        else:
            link = _http_url(entry.get("link"))
            if link:
                items.append(_external_row(title, sub, link, ART + "hardscore.png"))
    for entry in hardscore.events():
        title = _clean_text(entry.get("title"), MAX_TITLE)
        link = _http_url(entry.get("link"))
        if title and link:
            items.append(_external_row(title, _source_sub("Event", entry.get("date")),
                                       link, ART + "hardscore.png"))
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("Hardscore", window, page, total,
                                  "/api/hardscore"))


@app.get("/api/toronto")
def api_toronto():
    """Toronto Rave Mixtape Archive — genre directories and direct MP3s."""
    genre = (request.args.get("genre") or "").strip().lower()
    query = _clean_text(request.args.get("q", ""), MAX_QUERY)
    limit_arg = request.args.get("limit")
    try:
        limit = None if limit_arg in (None, "") else max(1, min(int(limit_arg), 5000))
    except (TypeError, ValueError):
        limit = None
    if query:
        entries = toronto.search(query, limit=limit)
        title = "Toronto Rave Mixtape Archive: %s" % query
        base = "/api/toronto?q=%s" % urllib.parse.quote(query)
    elif genre:
        entry = toronto.GENRE_BY_ID.get(genre)
        if not entry:
            return _json_error("unknown Toronto genre", 404)
        entries = toronto.genre_items(genre, limit=limit)
        title = "Toronto Rave Mixtape Archive: %s" % entry["label"]
        base = "/api/toronto?genre=%s" % urllib.parse.quote(genre)
    else:
        items = []
        for entry in toronto.genres():
            items.append(_folder(
                entry["label"], "Direct MP3s · Toronto Rave Mixtape Archive",
                "/api/toronto?genre=%s" % urllib.parse.quote(entry["id"]),
                ART + "toronto.png"))
        return jsonify({"title": "Toronto Rave Mixtape Archive", "items": items})
    # Filter once, cheaply, then build item dicts only for the visible window:
    # building all 2,640 Toronto rows on every page cost ~37 ms/page even
    # though only 40 rows are sent.
    valid = []
    for entry in entries:
        name = _clean_text(entry.get("title"), MAX_TITLE)
        url = _http_url(entry.get("url"))
        if name and url:
            valid.append((entry, name, url))

    def build(rows):
        items = []
        for entry, name, url in rows:
            sub = _source_sub("Toronto", entry.get("date"), entry.get("venue"))
            item = _track(name, sub, url, ART + "toronto.png",
                          {"kind": "stream", "target": url, "title": name})
            item["date"] = entry.get("date") or ""
            item["venue"] = entry.get("venue") or ""
            item["duration"] = entry.get("duration") or ""
            _mark_download(item, "toronto", url)
            items.append(item)
        return items

    if limit is not None:
        # Explicit `?limit=` callers keep the old "give me a capped list"
        # contract; the app itself always pages (?page=).
        return jsonify({"title": title, "items": build(valid[:limit])})
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    return jsonify(_paged_payload(title, build(window), page, total, base))


@app.get("/api/ravearchive")
def api_ravearchive():
    """Rave Archive UK releases (year/DJs from categories), each with a player."""
    items = []
    for entry in ravearchive.releases():
        title = _clean_text(entry.get("title"), MAX_TITLE)
        slug = entry.get("slug") or ""
        if not title or not slug:
            continue
        djs = ", ".join(entry.get("djs") or [])[:90]
        items.append(_folder(title, _source_sub(entry.get("year"), djs),
                             "/api/ravearchive/%s" % urllib.parse.quote(slug, safe=""),
                             ART + "rave-archive.png"))
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("Rave Archive UK", window, page, total,
                                  "/api/ravearchive"))


@app.get("/api/ravearchive/<path:slug>")
def api_ravearchive_release(slug):
    release = ravearchive.release(slug)
    if not release:
        return _json_error("unknown Rave Archive release", 404)
    title = _clean_text(release.get("title"), MAX_TITLE)
    link = _http_url(release.get("link"))
    cloudcast = release.get("mixcloud") or ""
    items = []
    if cloudcast:
        item = _track(title, "Mixcloud · Rave Archive UK", None, ART + "rave-archive.png")
        item["embed"] = mixcloud.embed_url(cloudcast)
        item["external"] = cloudcast
        items.append(item)
    elif link:
        items.append(_external_row(title, "Open on Rave Archive UK", link,
                                   ART + "rave-archive.png"))
    if not items:
        return _json_error("no player found for that release", 404)
    return jsonify({"title": title, "items": items})


@app.get("/api/getdarker")
def api_getdarker():
    """GetDarker sets: RSS embeds first, Mixcloud account as fallback."""
    items = []
    for entry in getdarker.sets():
        title = _clean_text(entry.get("title"), MAX_TITLE)
        if not title:
            continue
        cloudcast = _http_url(entry.get("mixcloud"))
        if cloudcast:
            item = _track(title, _source_sub("GetDarker", entry.get("date")), None,
                          ART + "getdarker.png")
            item["embed"] = mixcloud.embed_url(cloudcast)
            item["external"] = cloudcast
            items.append(item)
            continue
        media = _http_url(entry.get("media"))
        if media:
            items.append(_track(title, _source_sub("GetDarker", entry.get("date")),
                                media, ART + "getdarker.png",
                                {"kind": "stream", "target": media, "title": title}))
            continue
        link = _http_url(entry.get("link"))
        if link:
            items.append(_external_row(title, _source_sub("GetDarker", entry.get("date")),
                                       link, ART + "getdarker.png"))
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("GetDarker", window, page, total,
                                  "/api/getdarker"))


@app.get("/api/pirate")
def api_pirate():
    """Curated Pirate Radio Archives: one folder per station."""
    items = []
    for group in pirate.stations():
        sub = _source_sub("%d archive%s" % (group["count"], "" if group["count"] == 1 else "s"),
                          ", ".join(group.get("years") or []))
        items.append(_folder(group["station"], sub or "curated archive",
                             "/api/pirate/%s" % urllib.parse.quote(group["id"], safe=""),
                             ART + "pirate-archive.png"))
    return jsonify({"title": "Pirate Radio Archives", "items": items})


@app.get("/api/pirate/<slug>")
def api_pirate_station(slug):
    """Archive entries for one station; plays direct audio, links out otherwise."""
    entries = pirate.station(slug)
    if not entries:
        return _json_error("unknown pirate station", 404)
    items = []
    for entry in entries:
        title = _clean_text(entry.get("title"), MAX_TITLE)
        if not title:
            continue
        sub = _source_sub(entry.get("year"), entry.get("djs"))
        stream = _http_url(entry.get("stream_url"))
        source = _http_url(entry.get("source_url"))
        if stream:
            item = _track(title, sub, stream, ART + "pirate-archive.png",
                          {"kind": "stream", "target": stream, "title": title})
            _mark_download(item, "pirate", stream)
            if source:
                item["external"] = source
        elif source:
            item = _external_row(title, sub, source, ART + "pirate-archive.png")
        else:
            # Kept deliberately: the metadata is the archive, no dead link shown.
            item = _track(title, _source_sub(sub, "link pending"), None,
                          ART + "pirate-archive.png")
        if entry.get("credit"):
            item["credit"] = entry["credit"]
        items.append(item)
    return jsonify({"title": pirate.station(slug)[0]["station"], "items": items})


@app.get("/api/every-pack")
def api_every_pack():
    """Demoted archive.org collection: Every Pack / Archive mixtapes."""
    page = _page_arg()
    query = next((entry[2] for entry in ARCHIVE_SECTIONS if entry[0] == "tapepacks"),
                 "collection:ravetapes AND mediatype:audio")
    docs, total, page = _archive_search_page(query, page, ROWS)
    return jsonify(_paged_payload("Every Pack", _pack_items(docs), page, total,
                                  "/api/every-pack"))


@app.get("/api/packs")
def api_packs():
    """Tape Packs taxonomy hub (1.6.44 exact order)."""
    items = []
    for sid in ("toronto", "jungletapes", "ravetapepacks", "ezgarage",
                "20bensons", "ukraves", "classic-packs", "by-rave",
                "by-year", "every-pack"):
        section = next((entry for entry in SECTIONS if entry[0] == sid), None)
        if section:
            items.append(_folder(section[1], section[3], SECTION_PATHS[sid],
                                 ART + section[2]))
    return jsonify({"title": "Tape Packs", "items": items})


@app.get("/api/packs/<key>")
def api_pack_list(key):
    section = next((entry for entry in ARCHIVE_SECTIONS if entry[0] == key), None)
    if not section:
        return _json_error("unknown section", 404)
    page = _page_arg()
    docs, total, page = _archive_search_page(section[2], page, ROWS)
    return jsonify(_paged_payload(section[1], _pack_items(docs), page, total,
                                  "/api/packs/%s" % key))


# --------------------------------------------------------------------------- ez garage

def _ezgarage_row(set_id):
    return next((row for row in ezgarage.SETS if row["id"] == set_id), None)


def _ezgarage_error(exc):
    """Map a typed resolver failure to the app's JSON error shape."""
    return _stream_error(getattr(exc, "code", 502), exc.message,
                         retry=getattr(exc, "retry_after", None),
                         error_code="ezgarage")


@app.get("/api/ezgarage")
def api_ezgarage():
    """DNBForum's DJ EZ / old-skool garage thread: 23 Mediafire sets."""
    items = []
    for row in ezgarage.SETS:
        set_id = row["id"]
        play = "/api/ezgarage/%s/stream" % urllib.parse.quote(set_id, safe="")
        item = _track(row["title"], _source_sub("DJ EZ", ezgarage.SOURCE_LABEL),
                      play, ART + "djez.png",
                      {"kind": "stream", "target": play, "title": row["title"]})
        # Resolve the page server-side at download time; the client only ever
        # sends the curated set id, never a Mediafire URL.
        item["download"] = {"source": "mediafire", "id": set_id}
        item["file"] = "/api/ezgarage/%s/file" % urllib.parse.quote(set_id, safe="")
        items.append(item)
    return jsonify({
        "title": "EZ Garage Sets",
        "sub": "23 DJ EZ and old-skool garage sets from DNBForum",
        "source": ezgarage.SOURCE_URL,
        "credit": "Curated from the DNBForum thread (Wayback snapshot)",
        "items": items,
    })


@app.get("/api/ezgarage/<set_id>/stream")
def api_ezgarage_stream(set_id):
    """Resolve the Mediafire page, then proxy the signed MP3 same-origin."""
    row = _ezgarage_row(set_id)
    if row is None:
        return _ezgarage_error(ezgarage.UnknownSet("unknown DJ EZ set"))
    try:
        target = ezgarage.resolve(set_id)
    except ezgarage.EzGarageError as exc:
        return _ezgarage_error(exc)
    options = {"user-agent": ezgarage.BROWSER_UA,
               "referer": "https://www.mediafire.com/"}
    return _serve_stream(target, options, request.headers.get("Range"), None)


@app.get("/api/ezgarage/<set_id>/file")
def api_ezgarage_file(set_id):
    """Download one set as an attachment through the same signed-URL proxy."""
    row = _ezgarage_row(set_id)
    if row is None:
        return _ezgarage_error(ezgarage.UnknownSet("unknown DJ EZ set"))
    try:
        target = ezgarage.resolve(set_id)
    except ezgarage.EzGarageError as exc:
        return _ezgarage_error(exc)
    filename = downloads.safe_stem(row["title"]) + ".mp3"
    disposition = 'attachment; filename="%s"' % filename
    options = {"user-agent": ezgarage.BROWSER_UA,
               "referer": "https://www.mediafire.com/"}
    return _serve_stream(target, options, request.headers.get("Range"), None,
                         disposition=disposition)


# --------------------------------------------------------------------------- more web sources

def _ukraves_error(exc):
    return _stream_error(getattr(exc, "code", 502), exc.message,
                         error_code="ukraves")


def _mix_id(value):
    text = str(value or "").strip()
    return text if re.match(r"^\d{1,10}$", text) else ""


def _drive_label(row):
    title = _clean_text(row.get("title"), 160) if isinstance(row, dict) else ""
    if not title or "download" in title.lower():
        return "Google Drive MP3"
    return title


def _is_podomatic_url(url):
    try:
        host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "podomatic.com" or host.endswith(".podomatic.com")


def _podomatic_url(url):
    """Canonicalise Podomatic's pdcn.co short link to the podomatic.com host.

    The DejaVu feed wraps its enclosures in ``pdcn.co/e/<host>/<path>``, which
    301s to the podomatic.com URL.  Canonicalising here keeps the download
    allowlist on the documented host and saves a redirect on every play.
    """
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if host == "pdcn.co" and parts.path.startswith("/e/"):
        rest = parts.path[3:]
        target_host, _, target_path = rest.partition("/")
        if target_host.endswith(".podomatic.com") and target_path:
            return urllib.parse.urlunsplit(
                ("https", target_host, "/" + target_path, parts.query, ""))
    return url


@app.get("/api/20bensons")
def api_20bensons():
    """20bensons.com's 234 direct old-skool rave MP3s, in homepage order."""
    rows = bensons.entries()
    page = _page_arg()
    window, page, _pages, total = _slice_page(rows, page)
    items = []
    for row in window:
        title = _clean_text(row.get("title"), MAX_TITLE)
        url = _http_url(row.get("url"))
        if not title or not url:
            continue
        sub = _source_sub(row.get("event"), row.get("year"), bensons.SOURCE_LABEL)
        item = _track(title, sub or bensons.SOURCE_LABEL, url, ART + "20bensons.png",
                      {"kind": "stream", "target": url, "title": title})
        if row.get("year"):
            item["fact"] = row["year"]
        _mark_download(item, "20bensons", url)
        items.append(item)
    payload = _paged_payload("20bensons.com", items, page, total,
                             "/api/20bensons")
    payload["sub"] = bensons.summary(rows)
    payload["source"] = bensons.BASE
    payload["credit"] = "20bensons.com — old-skool rave tape MP3s"
    return jsonify(payload)


@app.get("/api/ukraves")
def api_ukraves():
    """UK Raves event downloads: Mediafire plays, YouTube hand-offs."""
    page = _page_arg()
    rows, has_next = ukraves.page_entries(page)
    described = ukraves.describe_many(rows)
    items = []
    for row in described:
        title = _clean_text(row.get("title"), MAX_TITLE) or "UK Raves mix"
        provider = row.get("provider") if isinstance(row.get("provider"), dict) else {}
        kind = provider.get("kind")
        sub = _source_sub(row.get("event") or row.get("subtitle"),
                          row.get("date"), ukraves.SOURCE_LABEL)
        if kind == "mediafire":
            mix_id = _mix_id(row.get("id"))
            if not mix_id:
                continue
            play = "/api/ukraves/%s/stream" % mix_id
            item = _track(title, sub, play, ART + "ukraves.png",
                          {"kind": "stream", "target": play, "title": title})
            item["file"] = "/api/ukraves/%s/file" % mix_id
            item["download"] = {"source": "ukraves", "id": mix_id}
        elif kind == "direct" and _http_url(provider.get("url")):
            target = _http_url(provider.get("url"))
            item = _track(title, sub, target, ART + "ukraves.png",
                          {"kind": "stream", "target": target, "title": title})
        else:
            # YouTube/external hand-offs opened a browser pop-up the owner does
            # not want in a downloads room; only real files are listed.
            continue
        items.append(item)
    pager = _cursor_pager(page, "/api/ukraves", has_next)
    payload = {
        "title": "UK Raves Downloads",
        "items": items,
        "pager": pager,
        "sub": ukraves.summary(rows),
        "source": ukraves.SOURCE_URL,
        "credit": ukraves.ATTRIBUTION,
    }
    if pager.get("next"):
        payload["next"] = pager["next"]
    return jsonify(payload)


@app.get("/api/ukraves/<mix_id>/stream")
def api_ukraves_stream(mix_id):
    """Resolve /go/mix -> Mediafire -> signed MP3 and proxy it."""
    wanted = _mix_id(mix_id)
    if not wanted:
        return _stream_error(404, "unknown UK Raves mix", error_code="ukraves")
    try:
        target = ukraves.mediafire_url(wanted)
    except ukraves.UkRavesError as exc:
        return _ukraves_error(exc)
    options = {"user-agent": ukraves.BROWSER_UA,
               "referer": "https://www.mediafire.com/"}
    return _serve_stream(target, options, request.headers.get("Range"), None)


@app.get("/api/ukraves/<mix_id>/file")
def api_ukraves_file(mix_id):
    """Download one UK Raves Mediafire mix as an attachment."""
    wanted = _mix_id(mix_id)
    if not wanted:
        return _stream_error(404, "unknown UK Raves mix", error_code="ukraves")
    try:
        target = ukraves.mediafire_url(wanted)
    except ukraves.UkRavesError as exc:
        return _ukraves_error(exc)
    disposition = 'attachment; filename="%s"' % (
        downloads.safe_stem("UK Raves mix %s" % wanted) + ".mp3")
    options = {"user-agent": ukraves.BROWSER_UA,
               "referer": "https://www.mediafire.com/"}
    return _serve_stream(target, options, request.headers.get("Range"), None,
                         disposition=disposition)


@app.get("/api/mikus")
def api_mikus():
    """Mikus Musik posts as folders; detail rows carry the embedded widgets."""
    posts = mikus.posts()
    page = _page_arg()
    window, page, _pages, total = _slice_page(posts, page)
    items = []
    for row in window:
        title = _clean_text(row.get("title"), MAX_TITLE)
        post_id = str(row.get("id") or "")
        if not title or not post_id:
            continue
        items.append(_folder(title, mikus.summary(row),
                             "/api/mikus/%s" % urllib.parse.quote(post_id, safe=""),
                             ART + "mikus.png"))
    payload = _paged_payload("Mikus Musik", items, page, total, "/api/mikus")
    payload["sub"] = ("%d posts · house, garage, jungle and soul mixes · Mikus Musik"
                      % len(posts)) if posts else "Source unavailable — try again later"
    payload["source"] = mikus.SOURCE_URL
    payload["credit"] = "Mikus Musik (mikusmusik.blogspot.com)"
    return jsonify(payload)


@app.get("/api/mikus/<post_id>")
def api_mikus_post(post_id):
    """One Mikus post: inline Mixcloud/SoundCloud, YouTube hand-off, Drive files."""
    row = mikus.post(post_id)
    if not row:
        return _json_error("unknown Mikus post", 404)
    title = _clean_text(row.get("title"), MAX_TITLE) or "Mikus Musik"
    items = []
    mixcloud_page_url = _http_url(row.get("mixcloud"))
    if mixcloud_page_url:
        item = _track("Mixcloud mix", "Inline Mixcloud widget · Mikus Musik",
                      None, ART + "mikus.png")
        item["embed"] = mikus.mixcloud_embed(mixcloud_page_url)
        item["external"] = mixcloud_page_url
        items.append(item)
    for url in row.get("soundcloud") or []:
        target = _http_url(url)
        if not target:
            continue
        item = _track("SoundCloud set", "Inline SoundCloud widget · Mikus Musik",
                      None, ART + "mikus.png")
        item["embed"] = mikus.soundcloud_embed(target)
        item["external"] = target
        items.append(item)
    for file_row in row.get("drive") or []:
        identifier = drive.file_id(file_row.get("id"))
        if not identifier:
            continue
        label = _drive_label(file_row)
        play = "/api/mikus/drive/%s/stream" % urllib.parse.quote(identifier, safe="")
        item = _track(label,
                      "Google Drive · play if direct or download · Mikus Musik",
                      None, ART + "mikus.png",
                      {"kind": "stream", "target": play, "title": label})
        item["play_via"] = "/api/mikus/drive/%s" % urllib.parse.quote(identifier, safe="")
        item["download"] = {"source": "mikus", "id": identifier}
        items.append(item)
    for url in row.get("youtube") or []:
        target = _http_url(url)
        if target:
            items.append(_external_row("YouTube video", "YouTube · hand-off · Mikus Musik",
                                       target, ART + "mikus.png"))
    for url in row.get("media") or []:
        target = _http_url(url)
        if target:
            item = _track("Direct MP3", "Mikus Musik", target,
                          ART + "mikus.png",
                          {"kind": "stream", "target": target,
                           "title": "Direct MP3"})
            _mark_download(item, "mikus", target)
            items.append(item)
    if not items and _http_url(row.get("url")):
        items.append(_external_row(title, "Open the post · Mikus Musik",
                                   _http_url(row.get("url")), ART + "mikus.png"))
    return jsonify({
        "title": title,
        "sub": _source_sub(row.get("published"), "Mikus Musik"),
        "source": _http_url(row.get("url")) or mikus.SOURCE_URL,
        "credit": "Mikus Musik (mikusmusik.blogspot.com)",
        "items": items,
    })


@app.get("/api/mikus/drive/<file_id>")
def api_mikus_drive(file_id):
    """Resolve a Drive file id to a direct audio URL for inline playback."""
    try:
        url = drive.resolve(file_id)
    except drive.DriveError as exc:
        return _json_error(exc.message, exc.code)
    return jsonify({"url": url, "title": "Google Drive MP3"})


@app.get("/api/mikus/drive/<file_id>/stream")
def api_mikus_drive_stream(file_id):
    """Stable same-origin playback URL for a Mikus Drive row.

    Favourites cannot store the short-lived signed URL, so the row's favourite
    target is this route: it resolves the file id again and proxies the audio
    through the same SSRF/Range path as every other stream.
    """
    try:
        url = drive.resolve(file_id)
    except drive.DriveError as exc:
        return _json_error(exc.message, exc.code)
    options = {"user-agent": drive.BROWSER_UA}
    return _serve_stream(url, options, request.headers.get("Range"), None)


@app.get("/api/demodulated")
def api_demodulated():
    """Demodulated Mixtapes posts, paginated as folders."""
    posts = demodulated.posts()
    page = _page_arg()
    window, page, _pages, total = _slice_page(posts, page)
    items = []
    for row in window:
        title = _clean_text(row.get("title"), MAX_TITLE)
        post_id = str(row.get("id") or "")
        if not title or not post_id:
            continue
        items.append(_folder(title, demodulated.summary(row),
                             "/api/demodulated/%s" % urllib.parse.quote(post_id, safe=""),
                             ART + "demodulated.png"))
    payload = _paged_payload("Demodulated Mixtapes", items, page, total,
                             "/api/demodulated")
    payload["sub"] = ("%d mixtapes · Toronto techno and rave sides · Demodulated"
                      % len(posts)) if posts else "Source unavailable — try again later"
    payload["source"] = demodulated.SOURCE_URL
    payload["credit"] = "Demodulated Mixtapes (mixtapes.demodulated.com)"
    return jsonify(payload)


@app.get("/api/demodulated/<post_id>")
def api_demodulated_post(post_id):
    """One mixtape: full sides play/download, samples are marked, ZIPs ignored."""
    row = demodulated.post(post_id)
    if not row:
        return _json_error("unknown Demodulated mixtape", 404)
    title = _clean_text(row.get("title"), MAX_TITLE) or "Demodulated Mixtape"
    items = []
    for track in row.get("tracks") or []:
        url = _http_url(track.get("url"))
        if not url:
            continue
        suffix = _clean_text(track.get("title"), 120) or "Track"
        label = "%s — %s" % (title, suffix) if suffix.lower() not in title.lower() else title
        sample = bool(track.get("sample"))
        sub = _source_sub("Sample" if sample else "Full side",
                          row.get("published"), "Demodulated Mixtapes")
        item = _track(label, sub, url, ART + "demodulated.png",
                      {"kind": "stream", "target": url, "title": label})
        if sample:
            # Samples are playable/favouritable previews, never a download.
            item["fact"] = "SAMPLE"
        else:
            _mark_download(item, "demodulated", url)
        items.append(item)
    if not items and _http_url(row.get("url")):
        items.append(_external_row(title, "ZIP only — open the source page",
                                   _http_url(row.get("url")), ART + "demodulated.png"))
    return jsonify({
        "title": title,
        "sub": _source_sub(row.get("published"), "Demodulated Mixtapes"),
        "source": _http_url(row.get("url")) or demodulated.SOURCE_URL,
        "credit": "Demodulated Mixtapes (mixtapes.demodulated.com)",
        "items": items,
    })


@app.get("/api/item/<path:identifier>")
def api_item(identifier):
    identifier = _clean_text(identifier, MAX_QUERY)
    if not identifier or "/" in identifier:
        return _json_error("unknown item", 404)
    tracks = archive.audio_files(identifier)
    if not tracks:
        return _json_error("no playable audio for that item", 404)
    # Audit P0-4: one Archive item can hold thousands of tracks
    # (helter-skelter-tape-packs: 1,645).  Page it like every other list so a
    # pack detail is bounded and the SPA's "Load more" can walk the rest.
    page = _page_arg()
    total = len(tracks)
    window, page, _pages, total = _slice_page(tracks, page)
    thumb = archive.thumb(identifier)
    items = []
    for track in window:
        item = _track(track["title"], _human_duration(track.get("length")), track["url"],
                      thumb,
                      {"kind": "stream", "target": track["url"], "title": track["title"]})
        _mark_download(item, "archive", track["url"])
        items.append(item)
    payload = _paged_payload(
        identifier.replace("-", " ").replace("_", " "), items, page, total,
        "/api/item/%s" % urllib.parse.quote(identifier), art=thumb)
    return jsonify(payload)


@app.get("/api/podcasts")
def api_podcasts():
    entries = feeds.registry()
    # Cached body + count: this list only needs the number, not the full
    # 200-episode document for every feed on every load.  Counts are
    # independent, so they are fetched in a bounded pool rather than one
    # after another (a cold /api/podcasts used to add five feed timeouts).
    counts = feeds.counts([entry["id"] for entry in entries])
    items = [
        _folder(entry["name"], "%d episodes" % counts.get(entry["id"], 0),
                "/api/podcast/%s" % urllib.parse.quote(entry["id"]),
                ART + "podcasts.png")
        for entry in entries
    ]
    return jsonify({"title": "Podcasts & Mixes", "items": items})


@app.get("/api/podcast/<feed_id>")
def api_podcast(feed_id):
    entry = feeds.feed(feed_id)
    if not entry:
        return _json_error("unknown feed", 404)
    valid = []
    for episode in feeds.episodes(feed_id):
        url = _podomatic_url(episode.get("url"))
        if isinstance(url, str) and url:
            title = _clean_text(episode.get("title"), MAX_TITLE) or "Untitled"
            valid.append((episode, title, url))
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    items = []
    for episode, title, url in window:
        item = _track(title, (episode.get("published") or "")[:16], url,
                      ART + "podcasts.png",
                      {"kind": "stream", "target": url, "title": title})
        if _is_podomatic_url(url):
            _mark_download(item, "podomatic", url)
        items.append(item)
    base = "/api/podcast/%s" % urllib.parse.quote(feed_id)
    return jsonify(_paged_payload(entry["name"], items, page, total, base))


@app.get("/api/dnb")
def api_dnb():
    # One provider call: mixes() re-parses on every call, and two calls could
    # disagree if the page changed between them (the old zip() mis-paired ids).
    shows = dnbradio.mixes()
    items = []
    for show in shows:
        if not isinstance(show, dict) or show.get("id") is None:
            continue
        title = _clean_text(show.get("title"), MAX_TITLE) or "Untitled"
        item = _track(title, _clean_text(show.get("date"), 40), None, ART + "dnb.png",
                      {"kind": "dnb", "target": str(show["id"]), "title": title})
        item["play_via"] = "/api/dnb/%s" % urllib.parse.quote(str(show["id"]))
        items.append(item)
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("DNB Radio", window, page, total,
                                  "/api/dnb"))


@app.get("/api/dnb/<mix_id>")
def api_dnb_resolve(mix_id):
    if not re.match(r"^\d{1,12}$", mix_id or ""):
        return _json_error("could not resolve mix", 404)
    key = "dnb:resolve:%s" % mix_id
    url = cache.remember(key, 3600, lambda: dnbradio.stream_url(mix_id) or None)
    if not url:
        return _json_error("could not resolve mix", 404)
    return jsonify({"url": url})


@app.get("/api/years")
def api_years():
    this_year = datetime.date.today().year
    items = [_folder(str(year), "Tape packs", "/api/year/%d" % year, ART + "years.png")
             for year in range(this_year, MIN_YEAR - 1, -1)]
    return jsonify({"title": "By Year", "items": items})


@app.get("/api/year/<int:year>")
def api_year(year):
    this_year = datetime.date.today().year
    if year < MIN_YEAR or year > this_year + 1:
        return _json_error("unknown year", 404)
    page = _page_arg()
    docs, total, page = _archive_search_page(latest.year_query(year), page, ROWS)
    return jsonify(_paged_payload(str(year), _pack_items(docs), page, total,
                                  "/api/year/%d" % year))


@app.get("/api/legacy/ravetapepacks")
def api_legacy_rtp():
    """RaveTapePacks.com: its own Event/DJ/MC/Year indexes."""
    if request.args.get("all"):
        return jsonify({"title": "RaveTapePacks.com", "items": [
            _folder(entry.get("name") or entry["key"],
                    _source_sub("RaveTapePacks.com", "%d tapes" % len(entry.get("tracks") or [])),
                    "/api/legacy/ravetapepacks/%s" % urllib.parse.quote(entry["key"]),
                    ART + "ravetapepacks.png")
            for entry in legacy.packs()
            if isinstance(entry, dict) and entry.get("key")]})
    cats = legacy.categories_state()
    if cats is None:
        # Audit P0-3: never report HTTP 200 with five "(0)" folders when the
        # homepage timed out.  The SPA renders this as a non-modal error plate
        # with Retry, and the 60 s negative cache keeps a retry cheap.
        return _json_error("RaveTapePacks is unavailable right now", 502,
                           error_code="source_unavailable")
    items = [
        _folder("By Event (%d)" % len(cats.get("events", [])),
                "Dreamscape, One Nation, Helter Skelter, Roast…",
                "/api/legacy/ravetapepacks/index/events", ART + "rave-nights.png"),
        _folder("By DJ (%d)" % len(cats.get("djs", [])),
                "Brockie, Randall, Hype, Slipmatt, SY…",
                "/api/legacy/ravetapepacks/index/djs", ART + "by-dj.png"),
        _folder("By MC (%d)" % len(cats.get("mcs", [])),
                "Det, Fearless, Skibadee, Bassman, Five-O…",
                "/api/legacy/ravetapepacks/index/mcs", ART + "by-mc.png"),
        _folder("By Year (%d)" % len(cats.get("years", [])),
                "1992 · 1993 · … · 2004",
                "/api/legacy/ravetapepacks/index/years", ART + "years.png"),
        _folder("All Packs", "Every RaveTapePacks pack",
                "/api/legacy/ravetapepacks/all", ART + "ravetapepacks.png"),
    ]
    return jsonify({"title": "RaveTapePacks.com", "items": items})


@app.get("/api/legacy/ravetapepacks/all")
def api_legacy_rtp_all():
    """The complete RaveTapePacks archive, paginated.

    The homepage only carries a rotating "RANDOM SELECTION" block, so the old
    implementation served a different handful of packs on every refresh and
    called itself "All Packs".  ``?cat=0`` is the site's blog archive: every
    post, in date order, with WordPress pagination.
    """
    page = _page_arg()
    posts, nxt = legacy.all_packs(page)
    if posts is None:
        return _json_error("RaveTapePacks index unavailable", 502)
    if not posts and page == 1:
        return _json_error("no packs in the RaveTapePacks archive", 404)
    items = [
        _folder(post.get("name") or "Pack %s" % post["id"],
                _source_sub("RaveTapePacks.com", "pack"),
                "/api/legacy/ravetapepacks/post/%s" % urllib.parse.quote(post["id"]),
                ART + "ravetapepacks.png",
                {"kind": "legacy_post", "target": post["id"],
                 "title": post.get("name") or "Pack %s" % post["id"]})
        for post in posts if isinstance(post, dict) and post.get("id")]
    payload = {"title": "RaveTapePacks.com", "items": items,
               "pager": _cursor_pager(page, "/api/legacy/ravetapepacks/all",
                                      bool(nxt))}
    if nxt:
        payload["next"] = "/api/legacy/ravetapepacks/all?page=%d" % (page + 1)
    return jsonify(payload)


@app.get("/api/legacy/ravetapepacks/index/<kind>")
def api_legacy_rtp_index(kind):
    if kind not in ("events", "djs", "mcs", "years"):
        return _json_error("unknown RaveTapePacks index", 404)
    categories = legacy.categories().get(kind, [])
    if not categories:
        return _json_error("RaveTapePacks index unavailable", 502)
    labels = {"events": "By Event", "djs": "By DJ", "mcs": "By MC", "years": "By Year"}
    items = [
        _folder(entry["name"], "RaveTapePacks.com",
                "/api/legacy/ravetapepacks/index/%s/%s" % (kind, entry["id"]),
                ART + {"events": "rave-nights.png", "djs": "by-dj.png",
                       "mcs": "by-mc.png", "years": "years.png"}[kind])
        for entry in categories]
    base = "/api/legacy/ravetapepacks/index/%s" % kind
    if request.args.get("limit") not in (None, ""):
        # Explicit `?limit=&offset=` callers keep the old slice contract.
        page_items, nxt = _optional_slice(items, base)
        payload = {"title": "%s · RaveTapePacks.com" % labels[kind],
                   "items": page_items}
        if nxt:
            payload["next"] = nxt
        return jsonify(payload)
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    return jsonify(_paged_payload("%s · RaveTapePacks.com" % labels[kind],
                                  window, page, total, base))


def _rtp_category_name(kind, cat_id):
    """Best-effort name for a category id from the already-cached nav.

    Only the title needs the navigation, so this must never add a homepage
    fetch to a category view (a cold category link is one page, not two).
    """
    try:
        return next((item["name"] for item in legacy.cached_categories().get(kind, [])
                     if item["id"] == str(cat_id)), "")
    except Exception:  # noqa: BLE001 - a category view must not depend on the nav
        return ""


@app.get("/api/legacy/ravetapepacks/index/<kind>/<cat_id>")
def api_legacy_rtp_category(kind, cat_id):
    """One page of a RaveTapePacks category, with a link to the next page."""
    if kind not in ("events", "djs", "mcs", "years") or not cat_id.isdigit():
        return _json_error("unknown RaveTapePacks category", 404)
    # Never fetch the homepage just to validate a deep category link: when the
    # hub is known bad this route must still answer from its own cat page
    # (audit P0-3 "child routes do not start another timeout").
    known = {entry["id"] for entry in legacy.cached_categories().get(kind, [])
             if isinstance(entry, dict) and entry.get("id")}
    if known and cat_id not in known:
        return _json_error("unknown RaveTapePacks category", 404)
    page = _page_arg()
    posts, nxt = legacy.category_page(cat_id, page)
    if posts is None:
        return _json_error("RaveTapePacks category unavailable", 502)
    if not posts and page == 1:
        return _json_error("no packs in that RaveTapePacks category", 404)
    name = _rtp_category_name(kind, cat_id)
    title = name or "RaveTapePacks.com · category"
    items = [
        _folder(post.get("name") or "Pack %s" % post["id"],
                _source_sub("RaveTapePacks.com", name or "category"),
                "/api/legacy/ravetapepacks/post/%s" % urllib.parse.quote(post["id"]),
                ART + "ravetapepacks.png",
                {"kind": "legacy_post", "target": post["id"],
                 "title": post.get("name") or "Pack %s" % post["id"]})
        for post in posts if isinstance(post, dict) and post.get("id")]
    base = "/api/legacy/ravetapepacks/index/%s/%s" % (kind, cat_id)
    payload = {"title": title, "items": items,
               "pager": _cursor_pager(page, base, bool(nxt))}
    if nxt:
        payload["next"] = ("/api/legacy/ravetapepacks/index/%s/%s?page=%d"
                           % (kind, cat_id, page + 1))
    return jsonify(payload)


def _rtp_pack_entry(pack):
    """Find one pack via the category that linked it, else the homepage index.

    ``core.legacy.pack()`` owns the cache lookup; a category link therefore
    avoids the full homepage index parse on a cold pack detail.
    """
    kind = request.args.get("kind", "")
    cat_id = request.args.get("cat", "")
    return legacy.pack(pack, kind=kind, cat_id=cat_id)


@app.get("/api/legacy/ravetapepacks/post/<post_id>")
def api_legacy_rtp_post(post_id):
    """One RaveTapePacks post page: its direct MP3s are the pack's tracks.

    1.6.53 P0-2: a failed fetch is a source outage (502, quiet plate), not a
    missing pack (404).  The SPA already renders ``source_unavailable``; the
    old mapping told the user that a valid pack had disappeared.
    """
    if not re.match(r"^\d{1,12}$", post_id or ""):
        return _json_error("unknown post", 404)
    try:
        entry = legacy.post(post_id)
    except legacy.SourceUnavailable:
        return _json_error("RaveTapePacks is unavailable right now", 502,
                           error_code="source_unavailable")
    if not entry or not entry.get("tracks"):
        return _json_error("no tracks on that RaveTapePacks post", 404)
    title = entry.get("name") or "RaveTapePacks.com"
    valid = [(track, _clean_text(track.get("name"), MAX_TITLE) or "Tape",
              legacy.playable(track["url"]))
             for track in entry["tracks"]
             if isinstance(track, dict) and track.get("url")]
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    items = []
    for _track_row, name, playable in window:
        item = _track(name, _source_sub("RaveTapePacks.com", title), playable,
                      ART + "tapepacks.png",
                      {"kind": "stream", "target": playable, "title": name})
        _mark_download(item, "ravetapepacks", playable)
        items.append(item)
    return jsonify(_paged_payload(
        title, items, page, total,
        "/api/legacy/ravetapepacks/post/%s" % urllib.parse.quote(post_id)))


@app.get("/api/legacy/ravetapepacks/<path:pack>")
def api_legacy_rtp_pack(pack):
    entry = _rtp_pack_entry(pack)
    if not entry:
        return _json_error("unknown pack", 404)
    valid = [(track, _clean_text(track.get("name"), MAX_TITLE) or "Tape",
              legacy.playable(track["url"]))
             for track in (entry.get("tracks") or [])
             if isinstance(track, dict) and track.get("url")]
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    items = []
    for _track_row, name, playable in window:
        item = _track(name, "Tape", playable, ART + "tapepacks.png",
                      {"kind": "stream", "target": playable, "title": name})
        _mark_download(item, "ravetapepacks", playable)
        items.append(item)
    base = "/api/legacy/ravetapepacks/%s" % urllib.parse.quote(pack)
    return jsonify(_paged_payload(entry.get("name") or pack, items, page,
                                  total, base))


@app.get("/api/legacy/jungletapes")
def api_legacy_jt():
    """JungleTapes show list, paginated: the sitemap catalogue is 300+ shows."""
    page = _page_arg()
    shows = [show for show in legacy.jungle_shows()
             if isinstance(show, dict) and isinstance(show.get("key"), str)]
    window, page, _pages, total = _slice_page(shows, page)
    items = []
    for show in window:
        name = show.get("name") or show["key"]
        items.append(_folder(name, "JungleTapes.com",
                             "/api/legacy/jungletapes/%s" % urllib.parse.quote(show["key"]),
                             ART + "jungletapes.png",
                             {"kind": "legacy_jt", "target": show["key"], "title": name}))
    return jsonify(_paged_payload("JungleTapes.com", items, page, total,
                                  "/api/legacy/jungletapes"))


@app.get("/api/legacy/jungletapes/<path:show_key>")
def api_legacy_jt_show(show_key):
    show_key = _clean_text(show_key, MAX_QUERY)
    if not show_key or "/" in show_key:
        return _json_error("unknown show", 404)
    valid = [(track, _clean_text(track.get("name"), MAX_TITLE) or "Track")
             for track in legacy.jungle_tracks(show_key)
             if isinstance(track, dict) and track.get("url")]
    if not valid:
        return _json_error("no recordings for that show", 404)
    page = _page_arg()
    window, page, _pages, total = _slice_page(valid, page)
    items = [
        _mark_download(
            _track(name, "JungleTapes.com", track["url"], ART + "jungletapes.png",
                   {"kind": "stream", "target": track["url"], "title": name}),
            "jungletapes", track["url"])
        for track, name in window]
    base = "/api/legacy/jungletapes/%s" % urllib.parse.quote(show_key)
    return jsonify(_paged_payload(
        show_key.replace("-", " ").replace("_", " "), items, page, total, base))


def _embed_items(entries, provider, art):
    """Turn provider search results into embed rows (host validated upstream)."""
    items = []
    for entry in entries or []:
        item = _track(entry["title"], "%s · %s" % (provider, entry["user"]), None,
                      entry.get("thumb") or art)
        item["embed"] = entry["embed"]
        item["external"] = entry["url"]
        items.append(item)
    return items


def _search_soundcloud(query):
    return _embed_items(soundcloud.search(query, limit=10), "SoundCloud",
                        ART + "soundcloud.png")


def _search_mixcloud(query):
    return _embed_items(mixcloud.search(query, limit=12), "Mixcloud",
                        ART + "mixcloud.png")


@app.get("/api/search")
def api_search():
    query = _clean_text(request.args.get("q", ""), MAX_QUERY)
    if not query:
        return jsonify({"title": "Search", "items": []})
    page = _page_arg()
    docs, total, page = _archive_search_page(
        "%s AND %s" % (query, latest.RAVE_COLLECTION), page, ROWS)
    page_one = page == 1
    # Full-text Archive search is noisy; widen to all audio only on the first
    # page when the curated collection has almost nothing.  The widen query,
    # SoundCloud and Mixcloud are independent, so they run in the bounded pool
    # (a cold search used to pay each timeout one after another).
    widen_needed = page_one and total < 10
    calls, names = [], []
    if page_one and soundcloud.configured():
        calls.append(lambda: _search_soundcloud(query))
        names.append("soundcloud")
    if page_one:
        calls.append(lambda: _search_mixcloud(query))
        names.append("mixcloud")
    if widen_needed:
        # Widen by *title*, not every audio item: an unscoped
        # ``dreamscape AND mediatype:audio`` returns audiobooks, sound effects
        # and meditation tracks before the rave tapes.  Requiring the scene
        # subject/collections keeps the fallback on-genre.
        widen_query = (
            'title:(%s) AND mediatype:audio AND '
            '(collection:ravetapes OR collection:cassettetapes OR '
            'subject:(rave OR jungle OR hardcore OR "drum and bass" OR '
            '"old skool" OR garage))' % query)
        calls.append(lambda: archive.search(widen_query, rows=ROWS)[0])
        names.append("widen")
    results = dict(zip(names, _gather(calls)))
    if widen_needed:
        seen = {doc.get("identifier") for doc in docs if isinstance(doc, dict)}
        docs += [doc for doc in (results.get("widen") or [])
                 if isinstance(doc, dict) and doc.get("identifier") not in seen]
    items = _pack_items(docs)
    # Mixcloud/SoundCloud sets live only behind their widget, but they belong in
    # a search for a DJ or a club night — append them as embed items on page one.
    items += list(results.get("soundcloud") or [])
    items += list(results.get("mixcloud") or [])
    payload = _paged_payload("Search: %s" % query, items, page, total,
                             "/api/search?q=%s" % urllib.parse.quote(query))
    if widen_needed:
        # Page one already appended the title-scoped widen results; the site
        # disables "next" here so a second page cannot repeat them.
        payload.pop("next", None)
        payload["pager"]["next"] = None
    return jsonify(payload)


# --------------------------------------------------------------------------- favourites


def _fav_target(entry):
    """Return a playable URL for a stored stream favourite.

    Legacy tracks are stored with Kodi's ``|Referer=...`` suffix; /api/stream
    understands that syntax, so the value can be passed through unchanged.
    """
    target = entry.get("target")
    return target if isinstance(target, str) else ""


@app.get("/api/favourites")
def api_fav_list():
    items = []
    identity = _download_identity()
    try:
        entries = (favourites.all_entries()
                   if request.args.get("all") == "1" and session.get("role") == "admin"
                   else favourites.load(identity))
    except Exception as exc:  # best effort: a broken store is an empty list
        app.logger.warning("favourites load failed: %s", exc)
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = _clean_text(entry.get("title"), MAX_TITLE)
        target = _fav_target(entry)
        if not title or not target:
            continue
        try:
            key = favourites.key(entry)
        except Exception:
            continue
        if entry.get("kind") == "pack":
            item = _folder(title, "Tape pack",
                           "/api/item/%s" % urllib.parse.quote(target),
                           archive.thumb(target))
        elif entry.get("kind") == "legacy_post":
            item = _folder(title, "RaveTapePacks pack",
                           "/api/legacy/ravetapepacks/post/%s"
                           % urllib.parse.quote(target, safe=""),
                           ART + "ravetapepacks.png")
        elif entry.get("kind") == "legacy_jt":
            item = _folder(title, "JungleTapes show",
                           "/api/legacy/jungletapes/%s"
                           % urllib.parse.quote(target, safe=""),
                           ART + "jungletapes.png")
        elif entry.get("kind") == "dnb":
            item = _track(title, "DNB Radio", None, ART + "dnb.png")
            item["play_via"] = "/api/dnb/%s" % urllib.parse.quote(target, safe="")
        else:
            item = _track(title, "Show", target, ART + "radio.png")
        item["fav_key"] = key
        if entry.get("identity"):
            item["identity"] = entry["identity"]
        items.append(item)
    page = _page_arg()
    window, page, _pages, total = _slice_page(items, page)
    base = "/api/favourites"
    if request.args.get("all") == "1":
        base += "?all=1"
    return jsonify(_paged_payload("Favourites", window, page, total, base))


FAVOURITE_KINDS = ("pack", "stream", "legacy_post", "legacy_jt", "dnb")


def _fav_payload():
    """Validate a favourites POST body; returns (entry, error)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, "expected a JSON object"
    kind = payload.get("kind", "pack")
    if kind not in FAVOURITE_KINDS:
        return None, "unknown favourite kind"
    title = _clean_text(payload.get("title"), MAX_TITLE)
    target = _clean_text(payload.get("target"), MAX_URL)
    if not title:
        return None, "title is required"
    if not target:
        return None, "target is required"
    if kind == "stream":
        try:
            base = _split_kodi_target(target)[0]
        except ValueError:
            return None, "stream target must be an http(s) url"
        if base.startswith("/"):
            # Curated sources expose stable same-origin proxies
            # (/api/ezgarage/<id>/stream, /api/ukraves/<id>/stream,
            # /api/mikus/drive/<id>/stream, /api/downloads/<id>/stream).
            # Only app API paths are valid: never "//host" or a bare page.
            if (not base.startswith("/api/") or base.startswith("//")
                    or "\\" in base or "/../" in base or base.endswith("/..")):
                return None, "stream target must be an http(s) url or an /api/ path"
        else:
            try:
                parsed = urllib.parse.urlsplit(base)
            except ValueError:
                return None, "stream target must be an http(s) url"
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return None, "stream target must be an http(s) url"
    elif kind in ("legacy_post", "dnb"):
        if not re.match(r"^\d{1,12}$", target):
            return None, "invalid %s id" % kind
    elif kind == "legacy_jt":
        if "/" in target or ".." in target or len(target) > 300:
            return None, "invalid JungleTapes show key"
    elif kind == "pack":
        if "/" in target or ".." in target or len(target) > 200:
            return None, "invalid Archive identifier"
    return {"kind": kind, "target": target, "title": title}, None


@app.post("/api/favourites")
def api_fav_add():
    entry, error = _fav_payload()
    if error:
        return jsonify({"saved": False, "error": error}), 400
    try:
        saved = favourites.add(entry, _download_identity())
    except Exception as exc:  # read-only or full data dir
        app.logger.warning("favourites add failed: %s", exc)
        saved = False
    return jsonify({"saved": bool(saved)})


@app.delete("/api/favourites/<path:key>")
def api_fav_remove(key):
    key = _clean_text(key, MAX_URL)
    identity = _download_identity()
    try:
        existed = any(favourites.key(entry) == key
                      for entry in favourites.load(identity)
                      if isinstance(entry, dict))
        removed = favourites.remove(key, identity) if existed else False
    except Exception as exc:
        app.logger.warning("favourites remove failed: %s", exc)
        existed, removed = False, False
    if not existed:
        return _json_error("favourite not found", 404)
    return jsonify({"removed": bool(removed)})


# --------------------------------------------------------------------------- streaming


class UpstreamError(Exception):
    """Base class for proxy failures; the view maps these to JSON errors."""

    code = 502
    message = "upstream connection failed"


class BlockedURL(UpstreamError):
    code = 400
    message = "blocked or missing url"


class UpstreamTimeout(UpstreamError):
    code = 504
    message = "upstream timed out"


def _split_kodi_target(value):
    """Split Kodi's ``url|Referer=...&User-Agent=...`` syntax.

    Legacy providers append this suffix to hotlink-protected URLs.  Browsers
    cannot turn it into headers, so the proxy does.  A plain URL without a
    suffix is returned untouched.
    """
    value = str(value or "").strip()
    if "|" not in value:
        return value, {}
    head, _, tail = value.partition("|")
    if not head or "=" not in tail:
        return value, {}
    options = {}
    for part in tail.split("&"):
        name, sep, raw = part.partition("=")
        if not sep:
            continue
        name = name.strip().lower()
        if name in ("referer", "user-agent") and raw:
            try:
                raw = urllib.parse.unquote(raw)
            except Exception:
                pass
            options[name] = raw
    return head.strip(), options


def _safe_header(value, limit):
    """Return a header-safe value, or '' if it contains controls/oversize."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > limit or _CTRL_RE.search(value):
        return ""
    return value


def _clean_range(value):
    """Only forward a single, well-formed byte range."""
    value = _safe_header(value, 64)
    if not value:
        return ""
    compact = value.replace(" ", "")
    if not _RANGE_RE.match(compact) or compact == "bytes=-":
        return ""
    return compact


def _request_target(url):
    """Percent-encode the path/query so non-ASCII URLs cannot break http.client."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return path + ("?" + query if query else "")


def _stream_host_allowed(host):
    """True when /api/stream may fetch ``host``.

    The default is to allow any public host (live radio streams live on
    arbitrary hosts).  RAVEWEB_STREAM_HOSTS turns /api/stream into a
    destination-scoped proxy instead of an open one.
    """
    if not STREAM_HOST_ALLOWLIST:
        return True
    host = str(host or "").lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in STREAM_HOST_ALLOWLIST)


def _check_port(port):
    if port < 1 or port > 65535:
        raise BlockedURL("bad port")
    if port < 1024 and port not in _ALLOWED_LOW_PORTS:
        raise BlockedURL("privileged port")
    if port in _BLOCKED_PORTS:
        raise BlockedURL("blocked port")


def _parse_target(url):
    """Validate scheme/host/port without DNS.  Returns (scheme, host, port)."""
    if not url or len(url) > MAX_URL:
        raise BlockedURL("missing or oversized url")
    if _CTRL_RE.search(url) or re.search(r"\s", url):
        raise BlockedURL("control characters in url")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise BlockedURL("unparsable url")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BlockedURL("bad scheme or host")
    if parsed.username is not None or parsed.password is not None:
        raise BlockedURL("credentials in url")
    host = parsed.hostname
    if not host or len(host) > 253:
        raise BlockedURL("bad host")
    try:
        port = parsed.port
    except ValueError:
        raise BlockedURL("bad port")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    _check_port(port)
    try:  # IP literals are already ASCII; IDNA only applies to names
        ipaddress.ip_address(host)
        encoded = host
    except ValueError:
        try:
            encoded = host.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            raise BlockedURL("bad host")
    if not _stream_host_allowed(encoded):
        raise BlockedURL("host not allow-listed")
    return parsed.scheme, encoded, port


def _ip_blocked(address):
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified):
        return True
    return any(address in network for network in _EXTRA_BLOCKED_NETWORKS)


def _resolve_target(url):
    """Resolve and validate a URL; returns (scheme, host, port, ip).

    Every address the hostname resolves to must be public.  The returned IP is
    the one the proxy connects to, so a second DNS answer cannot redirect the
    connection to a private address (DNS rebinding).
    """
    scheme, host, port = _parse_target(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not ALLOW_PRIVATE and _ip_blocked(literal):
            raise BlockedURL("private address")
        return scheme, host, port, str(literal)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise BlockedURL("host could not be resolved")
    if not infos:
        raise BlockedURL("host could not be resolved")
    addresses = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            raise BlockedURL("unusable address")
    if not ALLOW_PRIVATE and any(_ip_blocked(address) for address in addresses):
        raise BlockedURL("private address")
    # Prefer IPv4: some home networks have broken IPv6 routing.
    chosen = next((address for address in addresses if address.version == 4),
                  addresses[0])
    return scheme, host, port, str(chosen)


def _host_allowed(url):
    """Backwards-compatible boolean wrapper around the SSRF checks."""
    try:
        _resolve_target(url)
    except UpstreamError:
        return False
    return True


def _new_connection(scheme, host, port, ip):
    """Open a connection pinned to ``ip`` while keeping ``host`` for TLS/Host."""
    timeout = CONNECT_TIMEOUT
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_SSL_CONTEXT)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    def create(address, timeout=None, source_address=None):
        return socket.create_connection((ip, port), timeout, source_address)

    conn._create_connection = create
    return conn


def _proxy_parts(proxy):
    """Return (host, port, auth_headers) for a proxy URL, or None."""
    try:
        parts = urllib.parse.urlsplit(proxy)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    if not host or not 1 <= port <= 65535:
        return None
    auth = {}
    if parts.username is not None:
        raw = "%s:%s" % (urllib.parse.unquote(parts.username),
                         urllib.parse.unquote(parts.password or ""))
        auth["Proxy-Authorization"] = "Basic " + base64.b64encode(
            raw.encode("utf-8")).decode("ascii")
    return host, port, auth


def _new_proxy_connection(scheme, host, port, proxy):
    """Connection to ``proxy``; HTTPS targets are tunnelled with CONNECT.

    The SSRF checks already ran against the target in ``_resolve_target``.
    For a proxied hop the proxy performs its own DNS lookup, so the proxy is
    the trust boundary there: it only accepts RaveTapePacks hostnames.
    """
    parts = _proxy_parts(proxy)
    if not parts:
        raise OSError("invalid egress proxy URL")
    proxy_host, proxy_port, auth = parts
    if scheme == "https":
        conn = http.client.HTTPSConnection(
            proxy_host, proxy_port, timeout=CONNECT_TIMEOUT, context=_SSL_CONTEXT)
        conn.set_tunnel(host, port, headers=auth)
        return conn, auth
    return http.client.HTTPConnection(
        proxy_host, proxy_port, timeout=CONNECT_TIMEOUT), auth


def _upstream_headers(target, options, requested_range, ref_override):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        # Never let a gzip layer make Content-Length meaningless for audio.
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    user_agent = _safe_header(options.get("user-agent"), 200)
    if user_agent:
        headers["User-Agent"] = user_agent
    referer = _safe_header(options.get("referer") or ref_override, 512)
    if not referer:
        try:
            host = urllib.parse.urlsplit(target).hostname or ""
        except ValueError:
            host = ""
        referer = "https://%s/" % host
    headers["Referer"] = referer
    byte_range = _clean_range(requested_range)
    if byte_range:
        headers["Range"] = byte_range
    return headers


# Hosts whose hotlink guard has historically accepted a Referer but may
# tighten it to the exact pack page.  When the first attempt is refused, retry
# once with the file's own directory as Referer before giving up.
_REFERER_RETRY_HOSTS = ("ravetapepacks.com",)


def _referer_retry_headers(target, headers):
    """Return headers for one directory-Referer retry, or None when unsuitable."""
    try:
        split = urllib.parse.urlsplit(target)
    except ValueError:
        return None
    host = (split.hostname or "").lower()
    if not any(host == allowed or host.endswith("." + allowed)
               for allowed in _REFERER_RETRY_HOSTS):
        return None
    directory = split.path.rsplit("/", 1)[0] + "/"
    referer = "%s://%s%s" % (split.scheme or "https", split.netloc, directory)
    if headers.get("Referer") == referer:
        return None
    retry = dict(headers)
    retry["Referer"] = referer
    return retry


def _open_stream(url, headers, deadline):
    """GET url, following public redirects by hand.  Returns (conn, resp, url).

    RaveTapePacks hosts use the configured egress proxy (the site firewalls
    the home WAN IP); every other host keeps the direct pinned-IP path.  A
    dead proxy falls back to direct once per hop, inside the same deadline.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() >= deadline:
            raise UpstreamTimeout()
        try:
            scheme, host, port, ip = _resolve_target(current)
        except BlockedURL:
            if current != url:
                app.logger.warning("redirect to a blocked address from %s",
                                   _safe_log_url(url))
                raise UpstreamError()
            raise
        proxy = fetch_core.egress_proxy_for(current)
        attempts = [proxy, ""] if proxy else [""]
        conn = resp = None
        for proxy_url in attempts:
            auth = {}
            if proxy_url:
                try:
                    conn, auth = _new_proxy_connection(scheme, host, port, proxy_url)
                except OSError:
                    continue
            else:
                conn = _new_connection(scheme, host, port, ip)
            try:
                target = _request_target(current)
                request_headers = headers
                if proxy_url and scheme == "http":
                    # A plain-HTTP proxy expects the absolute URL; RTP itself
                    # is HTTPS, so this path is only for completeness/tests.
                    target = current
                    if auth:
                        request_headers = dict(headers)
                        request_headers.update(auth)
                conn.request("GET", target, headers=request_headers)
                resp = conn.getresponse()
                break
            except (socket.timeout, TimeoutError):
                conn.close()
                if proxy_url and len(attempts) > 1:
                    app.logger.info(
                        "RTP egress proxy timed out for %s; trying direct",
                        _safe_log_url(current))
                    continue
                raise UpstreamTimeout()
            except (http.client.HTTPException, ssl.SSLError, OSError, ValueError) as exc:
                conn.close()
                if proxy_url and len(attempts) > 1:
                    app.logger.info(
                        "RTP egress proxy unavailable for %s (%s); trying direct",
                        _safe_log_url(current), type(exc).__name__)
                    continue
                app.logger.info("upstream connect failed id=%s for %s: %s",
                                _current_request_id(), _safe_log_url(current), exc)
                raise UpstreamError()
        if resp is None:
            raise UpstreamError()
        if resp.status in _REDIRECT_CODES:
            location = resp.getheader("Location") or ""
            try:
                resp.close()
            except Exception:
                pass
            conn.close()
            if not location:
                raise UpstreamError()
            current = urllib.parse.urljoin(current, location)
            continue
        try:
            conn.sock.settimeout(READ_TIMEOUT)
        except (AttributeError, OSError):
            pass
        return conn, resp, current
    raise UpstreamError()


def _safe_log_url(url):
    """Keep credentials/query strings out of logs."""
    try:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return "<unparsable url>"


def _is_media_type(content_type):
    if not content_type:
        return True
    if content_type.startswith(("audio/", "video/")):
        return True
    return content_type in _MEDIA_EXACT


SOURCE_BLOCKED_MESSAGE = (
    "RaveTapePacks is temporarily blocking this connection — try again in a "
    "few minutes or reboot your router."
)


def _is_ravetapepacks_url(url):
    try:
        host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "ravetapepacks.com" or host.endswith(".ravetapepacks.com")


def _stream_error(code, message, retry=None, error_code=None):
    headers = {}
    if retry is not None:
        headers["Retry-After"] = str(retry)
    data = {"error": message}
    if error_code:
        data["code"] = error_code
    response = jsonify(data)
    response.status_code = code
    for name, value in headers.items():
        response.headers[name] = value
    return response


@app.get("/api/stream")
def api_stream():
    """Proxy the audio so Referer-guarded files and CORS/mixed-content work."""
    target, options = _split_kodi_target(request.args.get("u", ""))
    if not target:
        return _stream_error(400, "blocked or missing url")
    try:
        _parse_target(target)
    except UpstreamError:
        return _stream_error(400, "blocked or missing url")
    return _serve_stream(target, options, request.headers.get("Range"),
                         request.args.get("ref"))


def _serve_stream(target, options, requested_range=None, ref_override=None,
                  disposition=None):
    """Proxy one already-validated media URL.

    Shared by ``/api/stream`` and the curated EZ Garage routes so the same
    SSRF/redirect rules, Referer handling and stream caps apply to Mediafire.
    """
    # Shared with /api/mega/stream: every stream holds a worker thread, so one
    # process-wide budget keeps generic + MEGA streams from exceeding the
    # gthread count.  The per-generic cap remains as a narrower limit.
    if not stream_budget.acquire(timeout=2.0):
        return _stream_error(503, "too many streams in progress, retry shortly", retry=2)
    if not _STREAM_SLOTS.acquire(timeout=2.0):
        stream_budget.release()
        return _stream_error(503, "too many streams in progress, retry shortly", retry=2)

    released = [False]

    def release():
        if not released[0]:
            released[0] = True
            try:
                _STREAM_SLOTS.release()
            except ValueError:  # pragma: no cover - double release guard
                pass
            stream_budget.release()

    try:
        headers = _upstream_headers(target, options, requested_range,
                                    ref_override)
        deadline = time.monotonic() + STREAM_DEADLINE
        conn, resp, final_url = _open_stream(target, headers, deadline)
        if resp.status in (401, 403):
            # The hotlink guard may want the pack directory rather than the
            # site root.  One SSRF-checked retry with the stricter Referer is
            # cheap; a second refusal is reported normally below.
            retry = _referer_retry_headers(target, headers)
            if retry is not None:
                try:
                    resp.close()
                except Exception:
                    pass
                conn.close()
                conn, resp, final_url = _open_stream(target, retry, deadline)
                headers = retry
    except BlockedURL:
        release()
        return _stream_error(400, "blocked or missing url")
    except UpstreamTimeout:
        release()
        if _is_ravetapepacks_url(target):
            return _stream_error(504, SOURCE_BLOCKED_MESSAGE,
                                 error_code="source_blocked")
        return _stream_error(504, "upstream timed out")
    except UpstreamError:
        release()
        return _stream_error(502, "upstream connection failed")
    except Exception as exc:  # never leak a traceback to the client
        release()
        app.logger.warning("stream setup failed id=%s for %s: %s",
                           _current_request_id(), _safe_log_url(target), exc)
        return _stream_error(502, "upstream connection failed")

    status = resp.status
    try:
        if status in (401, 403):
            if _is_ravetapepacks_url(final_url or target):
                return _close_and_error(conn, resp, release, 403,
                                        SOURCE_BLOCKED_MESSAGE,
                                        error_code="source_blocked")
            return _close_and_error(conn, resp, release, 403, "upstream refused access")
        if status in (404, 410):
            return _close_and_error(conn, resp, release, 404, "upstream media not found")
        if status >= 500:
            return _close_and_error(conn, resp, release, 502, "upstream unavailable")
        if status == 416:
            headers_out = _passthrough_headers(resp)
            _close_quietly(conn, resp, release)
            return Response(b"", status=416, headers=headers_out)
        if status < 200 or status >= 300:
            return _close_and_error(conn, resp, release, 502, "unexpected upstream status")
        if (resp.getheader("Content-Encoding") or "").strip().lower() not in ("", "identity"):
            return _close_and_error(conn, resp, release, 502, "upstream sent an encoded stream")
        content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if not _is_media_type(content_type):
            app.logger.info("stream refused non-media content-type %r id=%s for %s",
                            content_type, _current_request_id(), _safe_log_url(final_url))
            return _close_and_error(conn, resp, release, 502, "upstream did not return audio")
    except Exception:
        _close_quietly(conn, resp, release)
        raise

    headers_out = _passthrough_headers(resp)
    headers_out.setdefault("Content-Type", content_type or "audio/mpeg")
    headers_out.setdefault("Cache-Control", "no-store")
    headers_out["Cache-Control"] = "no-store"
    # Audio is same-origin through this route, so a wildcard CORS header only
    # let other web pages read the proxy's responses; CORP closes that too.
    headers_out["Cross-Origin-Resource-Policy"] = "same-origin"
    headers_out["X-Content-Type-Options"] = "nosniff"
    if disposition:
        headers_out["Content-Disposition"] = disposition

    def generate():
        sent = 0
        try:
            while True:
                if time.monotonic() >= deadline:
                    app.logger.warning("stream deadline reached for %s",
                                       _safe_log_url(final_url))
                    break
                if sent >= MAX_STREAM_BYTES:
                    app.logger.warning("stream byte cap reached for %s",
                                       _safe_log_url(final_url))
                    break
                try:
                    chunk = resp.read1(MAX_CHUNK)
                except (socket.timeout, TimeoutError):
                    app.logger.info("upstream read timed out for %s",
                                    _safe_log_url(final_url))
                    break
                except (http.client.HTTPException, OSError, ValueError):
                    break
                if not chunk:
                    break
                sent += len(chunk)
                yield chunk
        finally:
            _close_quietly(conn, resp, release)

    response = Response(stream_with_context(generate()), status=status,
                        headers=headers_out, direct_passthrough=True)
    response.call_on_close(release)
    return response


def _passthrough_headers(resp):
    # Content-Length is deliberately not forwarded: generate() can stop early
    # (byte cap, deadline, upstream stall), and a stale length makes clients
    # wait for bytes that never arrive instead of retrying.  The response is
    # chunked, which clients handle for both 200 and 206 bodies.
    headers = {}
    for name in ("Content-Type", "Content-Range", "Accept-Ranges",
                 "Last-Modified", "ETag"):
        value = resp.getheader(name)
        if value:
            headers[name] = value
    return headers


def _close_quietly(conn, resp, release):
    try:
        resp.close()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    release()


def _close_and_error(conn, resp, release, code, message, headers_out=None,
                     error_code=None):
    _close_quietly(conn, resp, release)
    response = _stream_error(code, message, error_code=error_code)
    for name, value in (headers_out or {}).items():
        response.headers[name] = value
    return response


# --------------------------------------------------------------------------- pages


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True,
                    "version": APP_VERSION,
                    "revision": _build_revision(),
                    "data_writable": _data_writable()})


def _build_revision():
    """Commit baked into the image/deploy, or ``unknown`` when not supplied."""
    for name in ("RAVEWEB_REVISION", "RAVEWEB_GIT_COMMIT", "GIT_COMMIT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:64]
    return "unknown"


_DATA_PROBE = {"at": 0.0, "ok": True}


def _data_writable():
    """Best-effort, memoised check that favourites/cache can be written."""
    now = time.time()
    if now - _DATA_PROBE["at"] < 60:
        return _DATA_PROBE["ok"]
    ok = False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, prefix=".health-", delete=True):
            ok = True
    except OSError:
        ok = False
    _DATA_PROBE["at"] = now
    _DATA_PROBE["ok"] = ok
    return ok


@app.get("/")
def index():
    return render_template("index.html", version=APP_VERSION)


@app.get("/manifest.webmanifest")
@app.get("/manifest.json")
def manifest():
    """Installable PWA identity: `id`/`start_url`/`scope` are all the root.

    Without an explicit `id` and the apple-mobile-web-app meta tags in the
    template, iOS Safari's "Add to Home Screen" falls back to saving the
    current Safari tab as a bookmark — which is how a redirected ad page could
    become the home-screen target.
    """
    return jsonify({
        "id": "/",
        "name": "Rave Player",
        "short_name": "Rave Player",
        "description": ("Jungle, hardcore and rave tape packs, live radio and "
                        "podcasts from the Internet Archive."),
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#07070b",
        "theme_color": "#0a0a0f",
        "lang": "en-GB",
        "icons": [
            {"src": ART + "icon-180.png", "sizes": "180x180", "type": "image/png"},
            {"src": ART + "icon.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any"},
        ],
    }), 200, {"Content-Type": "application/manifest+json",
              "Cache-Control": "no-cache"}


@app.get("/sw.js")
def service_worker():
    """Serve the app-shell service worker at the root so scope is "/"."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "sw.js")
    response = send_file(path, mimetype="application/javascript",
                         conditional=False, max_age=0)
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


# --------------------------------------------------------------------------- headers / errors


@app.after_request
def _response_headers(response):
    response.headers.setdefault("X-Request-ID", getattr(g, "request_id", "") or secrets.token_hex(8))
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    is_html = response.mimetype == "text/html"
    if (request.path in ("/", "/login", "/logout")
            or request.path.startswith("/static/") or is_html):
        response.headers.setdefault("Content-Security-Policy", CSP)
        if CSP_REPORT_ONLY:
            response.headers.setdefault(
                "Content-Security-Policy-Report-Only",
                CSP + "; report-uri " + CSP_REPORT_URI
                + "; report-to " + CSP_REPORT_GROUP)
            report_to = {
                "group": CSP_REPORT_GROUP,
                "max_age": 10886400,
                "endpoints": [{"url": request.url_root.rstrip("/") + CSP_REPORT_URI}],
            }
            response.headers.setdefault("Report-To", json.dumps(report_to))
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.path.startswith("/static/"):
        # The template appends ?v=<VERSION> to the shell's CSS/JS, so a
        # versioned URL can never go stale: let the browser keep it for a year
        # instead of revalidating every asset on every launch.  Un-versioned
        # assets (artwork referenced by path) stay short-lived so a deploy
        # still refreshes them quickly.
        if request.args.get("v") == APP_VERSION:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=300"
    if _request_is_https():
        # Flask's session cookie is configured non-Secure so plain-HTTP LAN
        # installs keep working; upgrade the flag per-request behind TLS.
        cookies = response.headers.getlist("Set-Cookie")
        if cookies:
            response.headers.pop("Set-Cookie", None)
            for cookie in cookies:
                if "secure" not in cookie.lower():
                    cookie = cookie + "; Secure"
                response.headers.add("Set-Cookie", cookie)
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.after_request
def _log_request_line(response):
    """One structured line per page/API request, tagged with the request id.

    Path only: query strings can carry search terms or signed URLs, and the
    request id is the only correlation token the client needs. Static assets
    and health probes are skipped to keep the log readable.
    """
    try:
        path = request.path
        if (path.startswith("/static/")
                or path in ("/healthz", "/manifest.webmanifest", "/manifest.json", "/sw.js")):
            return response
        started = getattr(g, "request_started", None)
        elapsed = (time.perf_counter() - started) * 1000.0 if started else 0.0
        app.logger.info(
            "req id=%s method=%s path=%s status=%s ms=%.1f",
            _current_request_id(), request.method, path, response.status_code, elapsed)
    except Exception:  # logging must never break a response
        pass
    return response


_SHORT_ERRORS = {
    400: "bad request",
    404: "not found",
    405: "method not allowed",
    413: "request body too large",
    500: "internal error",
}


def _themed_error(code, exc=None):
    if code == 500:
        app.logger.error("unhandled error on %s", request.path,
                         exc_info=getattr(exc, "original_exception", None))
        title = "THE SIGNAL DROPPED"
        message = "The tape jammed on the server. Nothing was lost — turn the page over and try again."
    else:
        title = "LOST THE TAPE"
        message = "That reel is not in this archive. The track might have moved, or the link is older than the scene."
    try:
        return render_template("error.html", code=code, title=title,
                               message=message, version=APP_VERSION), code
    except Exception:  # a broken template must not turn a 500 into a crash
        app.logger.exception("themed error page failed for %s", code)
        return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Rave Player</title></head><body><h1>%s</h1>
<p>%s</p><p><a href="/">Back to the archive</a></p>
</body></html>""" % (title, message)), code


@app.errorhandler(HTTPException)
def _http_error(exc):
    code = exc.code or 500
    if request.path.startswith("/api/"):
        if code == 500:
            app.logger.error("unhandled error on %s", request.path,
                             exc_info=getattr(exc, "original_exception", None))
        return _json_error(_SHORT_ERRORS.get(code, exc.description or "error"), code)
    if code in (404, 500):
        return _themed_error(code, exc)
    return _SHORT_ERRORS.get(code, "request failed"), code
    return exc


# --------------------------------------------------------------------------- downloads


class DownloadError(Exception):
    """A proxy download failed; the view maps ``code``/``message`` to JSON."""

    def __init__(self, message, code=502):
        super(DownloadError, self).__init__(message)
        self.message = message
        self.code = code


class DownloadCancelled(Exception):
    """The user pressed Cancel; the partial file is discarded."""


_DOWNLOAD_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _download_cancelled(client_id):
    """Read the shared, cross-worker cancel flag (SQLite; see core/download_cancels)."""
    if not client_id:
        return False
    return download_cancels.is_cancelled(client_id)


def _human_size(size):
    try:
        size = max(0, int(size))
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024.0
    return "0 B"


def _download_allowed(source, url):
    """The single direct-file eligibility rule (shared with the Kodi add-on).

    Legacy providers store Kodi's ``|Referer=...`` suffix on a download URL;
    validate the clean target and let :func:`_download_chunks` turn the suffix
    back into headers.
    """
    if source not in DOWNLOAD_SOURCES:
        return False, "that source cannot be downloaded"
    if source == "ravetapepacks" and not RTP_DOWNLOADS_ENABLED:
        return False, "RaveTapePacks downloads are temporarily disabled"
    if not isinstance(url, str) or len(url) > MAX_URL:
        return False, "blocked or missing url"
    target, _options = _split_kodi_target(url)
    try:
        parts = urllib.parse.urlsplit(target)
    except ValueError:
        return False, "blocked or missing url"
    if parts.scheme not in ("http", "https"):
        return False, "only http(s) media can be downloaded"
    if parts.username or parts.password:
        return False, "urls with credentials cannot be downloaded"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False, "blocked or missing url"
    if any(host == blocked or host.endswith("." + blocked)
           for blocked in DOWNLOAD_BLOCKED_HOSTS):
        return False, "provider pages cannot be downloaded"
    drive_direct = (source == "mikus"
                    and host == "drive.usercontent.google.com"
                    and parts.path.rstrip("/") == "/download")
    if not drive_direct and not parts.path.lower().endswith(DOWNLOAD_EXTENSIONS):
        return False, "only direct MP3/M4A files can be downloaded"
    if source == "archive":
        if not (host == "archive.org" or host.endswith(".archive.org")):
            return False, "not an Internet Archive file"
    elif source == "moxser":
        if not (host == "enceladus.feralhosting.com" or host.endswith(".feralhosting.com")):
            return False, "not a Moxser file"
    elif source == "hardscore":
        if not (host == "hardscore.com" or host.endswith(".hardscore.com")):
            return False, "not a Hardscore file"
    elif source == "pirate":
        known = {_http_url(entry.get("stream_url")) for entry in pirate.entries()}
        if target not in known:
            return False, "not a curated pirate archive file"
    elif source == "toronto":
        if not (host == "torontoravemixtapearchive.com"
                or host.endswith(".torontoravemixtapearchive.com")):
            return False, "not a Toronto Rave Mixtape Archive file"
        if not (parts.path.startswith("/files/mixtape/")
                or parts.path.startswith("/files/xstatic/")):
            return False, "not a Toronto direct MP3"
    elif source == "ravetapepacks":
        if not (host == "ravetapepacks.com" or host.endswith(".ravetapepacks.com")):
            return False, "not a RaveTapePacks file"
        if not parts.path.startswith("/music/"):
            return False, "not a RaveTapePacks direct MP3"
    elif source == "jungletapes":
        if not (host == "jungletapes.com" or host.endswith(".jungletapes.com")):
            return False, "not a JungleTapes file"
    elif source in ("ezgarage", "mediafire"):
        # Mediafire's signed direct host only.  The file page itself
        # (www.mediafire.com/file/...) is never a download target; the job
        # resolves the curated set id server-side instead.
        if not (host == "mediafire.com" or host.endswith(".mediafire.com")):
            return False, "not a Mediafire file"
        if host in ("mediafire.com", "www.mediafire.com"):
            return False, "not a Mediafire direct file"
    elif source == "20bensons":
        if host != "20bensons.com":
            return False, "not a 20bensons.com file"
        if not parts.path.startswith("/mp3/"):
            return False, "not a 20bensons.com direct MP3"
    elif source == "ukraves":
        # Only the signed Mediafire file behind a UK Raves /go/mix redirect.
        if not (host == "mediafire.com" or host.endswith(".mediafire.com")):
            return False, "not a UK Raves Mediafire file"
        if host in ("mediafire.com", "www.mediafire.com"):
            return False, "not a Mediafire direct file"
    elif source == "mikus":
        if host in MIKUS_DIRECT_HOSTS:
            # Mikus posts its own MP3s on terraincognita.co.uk.  The exact
            # host is allowlisted; the extension check above still applies.
            pass
        elif host not in DRIVE_HOSTS:
            return False, "not a Mikus Musik or Google Drive file"
        elif host == "drive.usercontent.google.com":
            if parts.path.rstrip("/") != "/download":
                return False, "not a Google Drive direct file"
    elif source == "demodulated":
        if not (host == "demodulated.com" or host.endswith(".demodulated.com")):
            return False, "not a Demodulated Mixtapes file"
        if not parts.path.startswith("/music/mixsets/"):
            return False, "not a Demodulated mixset MP3"
    elif source == "podomatic":
        if not (host == "podomatic.com" or host.endswith(".podomatic.com")):
            return False, "not a Podomatic file"
    return True, ""


def _mark_download(item, source, url=None):
    """Expose the Download action only for eligible allowlisted rows."""
    if url is not None and not _download_allowed(source, url)[0]:
        return item
    item["download"] = {"source": source}
    return item


def _download_identity():
    return session.get("identity") or "owner"


def _download_item(entry):
    identifier = entry["id"]
    when = ""
    try:
        when = time.strftime("%d %b %Y", time.localtime(float(entry.get("created") or 0)))
    except (TypeError, ValueError, OverflowError, OSError):
        when = ""
    sub = _source_sub(_human_size(entry.get("size")),
                      when,
                      DOWNLOAD_SOURCE_LABELS.get(entry.get("source"), "Saved"))
    item = _track(entry.get("title") or "Untitled", sub,
                  "/api/downloads/%s/stream" % urllib.parse.quote(identifier, safe=""),
                  DOWNLOAD_SOURCE_ART.get(entry.get("source"), ART + "downloads.png"))
    item["download_key"] = identifier
    item["file"] = "/api/downloads/%s/file" % urllib.parse.quote(identifier, safe="")
    item["fav"] = {"kind": "stream", "target": item["play"],
                   "title": item["label"]}
    return item


def _source_stable_url(source, source_id):
    """Non-expiring library/dedupe URL for a server-resolved source.

    UK Raves stores the /go/mix page and Mikus stores the Drive view page;
    neither carries the short-lived signed URL that is resolved at play or
    download time.
    """
    identifier = _clean_text(source_id, 128)
    if source == "ukraves":
        mix_id = _mix_id(identifier)
        if not mix_id:
            raise DownloadError("a UK Raves mix id is required", 400)
        return "%s/go/mix/%s" % (ukraves.BASE, mix_id)
    if source == "mikus":
        wanted = drive.file_id(identifier)
        if not wanted:
            raise DownloadError("a Google Drive file id is required", 400)
        return drive.view_url(wanted)
    return ""


def _resolve_source_download(source, stable_url):
    """Resolve a stable server-side source URL to today's direct file URL."""
    if source == "ukraves":
        mix_id = ukraves.mix_id_for_url(stable_url)
        if not mix_id:
            raise DownloadError("unknown UK Raves mix", 400)
        try:
            return ukraves.mediafire_url(mix_id)
        except ukraves.UkRavesError as exc:
            raise DownloadError(exc.message, exc.code)
    if source == "mikus":
        wanted = drive.file_id(stable_url)
        if not wanted:
            raise DownloadError("unknown Google Drive file", 400)
        try:
            return drive.resolve(wanted)
        except drive.DriveError as exc:
            raise DownloadError(exc.message, exc.code)
    return ""


def _download_chunks(url, destination, max_bytes, seconds, should_cancel=None):
    """Proxy one direct audio file, yielding progress dicts.

    Reuses ``_open_stream`` so the same SSRF, DNS-pinning and redirect checks as
    /api/stream apply; every byte counts against the per-file and library caps.
    Yields ``{"started": True, "total": N|None}`` after the headers, then
    ``{"received": n, "total": ...}`` roughly ten times a second, then
    ``{"done": True, "size": n}``.  The caller owns cleanup of ``destination``.
    """
    target, options = _split_kodi_target(url)
    headers = _upstream_headers(target, options, None, None)
    deadline = time.monotonic() + seconds
    conn = resp = handle = None
    failed = True
    try:
        conn, resp, final_url = _open_stream(target, headers, deadline)
        status = resp.status
        if status in (401, 403):
            raise DownloadError("upstream refused access", 403)
        if status in (404, 410):
            raise DownloadError("upstream media not found", 404)
        if status != 200:
            raise DownloadError("unexpected upstream status", 502)
        encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            raise DownloadError("upstream sent an encoded file", 502)
        content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and not (content_type.startswith("audio/")
                                 or content_type == "application/octet-stream"):
            raise DownloadError("upstream did not return an audio file", 502)
        length_raw = resp.getheader("Content-Length")
        expected = None
        if length_raw:
            try:
                expected = int(length_raw)
            except (TypeError, ValueError):
                expected = None
            if expected is not None and expected > max_bytes:
                raise DownloadError("that file is larger than the download limit", 413)
        yield {"started": True, "total": expected}
        total = 0
        last_emit = 0.0
        last_cancel = 0.0
        handle = open(destination, "wb")
        while True:
            now = time.monotonic()
            # The shared cancel store is SQLite; polling it on every 64 KiB
            # chunk would open a connection thousands of times per file.
            # A 0.2 s cadence is still effectively immediate to the user.
            if should_cancel is not None and now - last_cancel >= 0.2:
                last_cancel = now
                if should_cancel():
                    raise DownloadCancelled()
            if now >= deadline:
                raise DownloadError("the download timed out", 504)
            try:
                chunk = resp.read(64 * 1024)
            except (socket.timeout, TimeoutError):
                raise DownloadError("the download timed out", 504)
            except http.client.IncompleteRead:
                raise DownloadError("upstream closed early — download incomplete", 502)
            if not chunk:
                break
            total += len(chunk)
            if expected is not None and total > expected:
                raise DownloadError("upstream sent more data than expected — "
                                    "download incomplete", 502)
            if total > max_bytes:
                raise DownloadError("that file is larger than the download limit", 413)
            handle.write(chunk)
            now = time.monotonic()
            if now - last_emit >= 0.1:
                last_emit = now
                yield {"received": total, "total": expected}
        handle.flush()
        os.fsync(handle.fileno())
        if total <= 0:
            raise DownloadError("upstream returned an empty file", 502)
        # QA 1.6.62: a truncated body can end with a clean EOF.  Never
        # publish/store a file whose byte count disagrees with Content-Length.
        if expected is not None and total != expected:
            raise DownloadError("upstream closed early — download incomplete", 502)
        failed = False
        yield {"done": True, "size": total}
    except (DownloadError, DownloadCancelled):
        raise
    except BlockedURL:
        raise DownloadError("blocked or invalid media URL", 400)
    except UpstreamTimeout:
        raise DownloadError("the download timed out", 504)
    except UpstreamError:
        raise DownloadError("upstream connection failed", 502)
    except (OSError, http.client.HTTPException, ssl.SSLError):
        raise DownloadError("could not save the download", 502)
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        # The caller owns the destination, but removing our own incomplete
        # temp file here means a failed/cancelled transfer can never leave a
        # partial file behind even if a caller forgets to discard it.
        if failed:
            try:
                os.remove(destination)
            except OSError:
                pass
        for closable in (resp, conn):
            if closable is not None:
                try:
                    closable.close()
                except Exception:  # noqa: BLE001 - close is best effort
                    pass


def _download_to_file(url, destination, max_bytes, seconds):
    """Consume ``_download_chunks`` without streaming and return the size."""
    size = 0
    for event in _download_chunks(url, destination, max_bytes, seconds):
        if event.get("done"):
            size = int(event.get("size") or 0)
    return size


# background jobs: persisted metadata + in-process worker, so closing the tab
# does not cancel the save.  The HTTP request only creates the job.
_DOWNLOAD_JOBS_LOCK = threading.RLock()
_JOB_SCHEDULER = {"started": False, "migrated": None,
                  "gate": threading.Event()}  # clear to pause test/work loops
_JOB_SCHEDULER["gate"].set()
_JOB_SCHEDULER_LOCK = threading.Lock()


def _jobs_ensure():
    """Start this process's dispatcher and clean up jobs from dead workers."""
    base = DATA_DIR
    with _DOWNLOAD_JOBS_LOCK:
        if _JOB_SCHEDULER["migrated"] != base:
            try:
                download_jobs.migrate_from_legacy_json(base)
            except Exception:
                app.logger.exception("jobs: legacy migration failed")
            _JOB_SCHEDULER["migrated"] = base
    try:
        download_jobs.recover_orphans()
    except Exception:
        app.logger.exception("jobs: orphan recovery failed")
    _JOB_SCHEDULER["gate"].set()
    with _JOB_SCHEDULER_LOCK:
        if _JOB_SCHEDULER["started"]:
            return
        _JOB_SCHEDULER["started"] = True
    thread = threading.Thread(target=_job_scheduler, name="download-job-scheduler",
                              daemon=True)
    thread.start()


def _job_scheduler():
    """Claim shared queued rows; the DB transaction enforces the global cap."""
    while True:
        try:
            if not _JOB_SCHEDULER["gate"].wait(timeout=0.4):
                continue
            while True:
                job = download_jobs.claim_next(DOWNLOAD_JOB_CONCURRENCY)
                if not job:
                    break
                threading.Thread(target=_job_worker, args=(job,),
                                 name="download-job-%s" % job["id"][:8],
                                 daemon=True).start()
        except Exception:
            app.logger.exception("jobs: scheduler tick failed")
        time.sleep(0.4)


def _job_get(job_id):
    _jobs_ensure()
    return download_jobs.get(job_id)


def _job_visible(job):
    if not job:
        return False
    return (job.get("identity") == (session.get("identity") or "")
            or session.get("role") == "admin")


def _job_cancel_requested(job_id):
    return download_jobs.cancel_requested(job_id)


def _job_cleanup_partial(job):
    if not job:
        return
    try:
        os.remove(download_jobs.temp_path(DATA_DIR, job))
    except OSError:
        pass


def _job_worker(job):
    job_id = job["id"]
    identity = job.get("identity") or "owner"
    entry = job.get("entry") or {}
    temp_name = download_jobs.temp_path(DATA_DIR, job)
    completed = False
    try:
        used = downloads.used(DATA_DIR, identity)
        max_bytes = min(DOWNLOAD_MAX_BYTES, max(0, DOWNLOAD_MAX_TOTAL - used))
        if max_bytes <= 0:
            raise DownloadError("the Downloads library is full", 413)
        source_url = entry["url"]
        source = entry.get("source")
        if source in ("ezgarage", "mediafire"):
            # Resolve at job time: the signed Mediafire URL expires, and the
            # queue may have waited behind other jobs.  The client only ever
            # provided the curated set id.
            set_id = ezgarage.set_id_for_url(source_url)
            if not set_id:
                raise DownloadError("unknown DJ EZ set", 400)
            try:
                source_url = ezgarage.resolve(set_id)
            except ezgarage.EzGarageError as exc:
                raise DownloadError(exc.message, exc.code)
        elif source in ("ukraves", "mikus"):
            # Only curated stable page/view URLs need resolving here; a Mikus
            # direct MP3 on terraincognita is already the file to fetch.
            stable = (ukraves.mix_id_for_url(source_url) if source == "ukraves"
                      else drive.file_id(source_url))
            if stable:
                source_url = _resolve_source_download(source, source_url)
            allowed, error = _download_allowed(source, source_url)
            if not allowed:
                raise DownloadError(error, 400)
        last_save = time.monotonic()
        for event in _download_chunks(
                source_url, temp_name, max_bytes, DOWNLOAD_DEADLINE,
                lambda: _job_cancel_requested(job_id)):
            if _job_cancel_requested(job_id):
                raise DownloadCancelled()
            if event.get("started"):
                download_jobs.heartbeat(job_id, bytes_total=event.get("total"),
                                        progress=0)
            elif event.get("received") is not None:
                total = event.get("total")
                done = max(0, int(event.get("received") or 0))
                percent = int(done * 100 / total) if total else 0
                now = time.monotonic()
                if now - last_save >= 0.5:
                    download_jobs.heartbeat(job_id, bytes_done=done,
                                            bytes_total=total,
                                            progress=max(0, min(100, percent)))
                    last_save = now
            elif event.get("done"):
                if download_jobs.cancel_requested(job_id):
                    raise DownloadCancelled()
                entry["size"] = int(event.get("size") or 0)
                downloads.register(DATA_DIR, entry, temp_name,
                                   DOWNLOAD_MAX_TOTAL, DOWNLOAD_MAX_ITEMS,
                                   identity)
                if download_jobs.complete(job_id, entry["size"], entry["id"]):
                    completed = True
                else:
                    downloads.remove(DATA_DIR, entry["id"], identity)
    except DownloadCancelled:
        download_jobs.fail(job_id, "cancelled", state="cancelled")
    except DownloadError as exc:
        download_jobs.fail(job_id, exc.message)
    except downloads.StorageError as exc:
        download_jobs.fail(job_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - never kill the worker silently
        app.logger.warning("background download %s failed: %s", job_id, exc)
        download_jobs.fail(job_id, "could not save that download")
    finally:
        if not completed:
            _job_cleanup_partial(job)


@app.post("/api/downloads/jobs")
def api_download_job_create():
    """Queue a server-side download; the request returns immediately."""
    _jobs_ensure()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_error("expected a JSON object", 400)
    source = _clean_text(payload.get("source"), 32).lower()
    url = _clean_text(payload.get("url"), MAX_URL)
    title = _clean_text(payload.get("title"), MAX_TITLE)
    if source in ("ezgarage", "mediafire"):
        # Store the curated page URL as the stable identity; the worker
        # resolves a fresh signed URL when the job actually starts.
        set_id = _clean_text(payload.get("source_id") or payload.get("id"), 32).lower()
        if not set_id:
            return _json_error("a curated set id is required", 400)
        try:
            row = ezgarage.entry(set_id)
        except ezgarage.UnknownSet:
            return _json_error("unknown DJ EZ set", 400)
        url = ezgarage.page_url(set_id)
        if not title:
            title = row["title"]
    elif (source in ("ukraves", "mikus")
          and (payload.get("source_id") or payload.get("id"))):
        # Stable id only; the worker resolves the signed URL when it starts.
        try:
            url = _source_stable_url(source, payload.get("source_id") or payload.get("id"))
        except DownloadError as exc:
            return _json_error(exc.message, exc.code)
        if not title:
            title = "UK Raves mix" if source == "ukraves" else "Mikus Musik MP3"
    else:
        allowed, error = _download_allowed(source, url)
        if not allowed:
            return _json_error(error, 400)
    identity = session.get("identity") or ""
    role = session.get("role") or ""
    identity = _download_identity()
    existing = downloads.find_url(DATA_DIR, url, identity)
    if existing:
        return jsonify({"id": existing["id"], "already": True,
                        "state": "completed", "job": None})
    active = [job for job in download_jobs.list_jobs(identity, role)
              if job.get("state") in ("queued", "downloading")]
    for job in active:
        if (job.get("identity") == identity
                and (job.get("entry") or {}).get("url") == url):
            return jsonify({"id": job["id"], "already": True,
                            "state": job["state"],
                            "job": download_jobs.public(job)}), 200
    if len(active) >= DOWNLOAD_JOB_MAX:
        return _json_error("too many background downloads", 429)
    try:
        downloads.reserve(DATA_DIR, DOWNLOAD_MAX_ITEMS, identity)
    except downloads.StorageError as exc:
        return _json_error(str(exc), 507)
    if not title:
        title = urllib.parse.unquote(url.rsplit("/", 1)[-1]) or "Download"
    entry = downloads.entry_for(DATA_DIR, title, url, source)
    job = download_jobs.create(identity, role, title, source, url, entry)
    _jobs_ensure()
    return jsonify({"id": job["id"], "job": download_jobs.public(job)}), 202


@app.get("/api/downloads/jobs")
def api_download_jobs():
    _jobs_ensure()
    role = session.get("role") or ""
    identity = session.get("identity") or ""
    jobs = [download_jobs.public(job)
            for job in download_jobs.list_jobs(identity, role)
            if _job_visible(job)]
    return jsonify({"jobs": jobs, "concurrency": DOWNLOAD_JOB_CONCURRENCY})


@app.get("/api/downloads/jobs/<job_id>")
def api_download_job(job_id):
    identifier = _clean_text(job_id, 64)
    job = _job_get(identifier)
    if not _job_visible(job):
        return _json_error("unknown download job", 404)
    return jsonify({"job": download_jobs.public(job)})


@app.post("/api/downloads/jobs/<job_id>/cancel")
def api_download_job_cancel(job_id):
    identifier = _clean_text(job_id, 64)
    job = _job_get(identifier)
    if not _job_visible(job):
        return _json_error("unknown download job", 404)
    if job.get("state") in ("completed", "failed", "cancelled"):
        return _json_error("download job is already finished", 409)
    updated = download_jobs.cancel(identifier, session.get("identity"),
                                   session.get("role"))
    if updated is None:
        return _json_error("unknown download job", 404)
    if updated.get("state") != "cancelled":
        return _json_error("download job is already finished", 409)
    _job_cleanup_partial(updated)
    return jsonify({"id": identifier, "state": "cancelled",
                    "job": download_jobs.public(updated)})


def _download_stream(url, entry, client_id, identity="owner"):
    """NDJSON progress stream for one explicit download (panel wiring).

    Emits ``started`` (with Content-Length when known), ``progress`` ticks,
    then exactly one of ``saved``/``cancelled``/``error``.  The partial file is
    discarded on any non-saved exit, including client disconnects.
    """
    temp_name = downloads.temp_path(DATA_DIR, entry, identity)
    saved = [False]

    def generate():
        try:
            used = downloads.used(DATA_DIR, identity)
            max_bytes = min(DOWNLOAD_MAX_BYTES, max(0, DOWNLOAD_MAX_TOTAL - used))
            if max_bytes <= 0:
                raise DownloadError("the Downloads library is full", 413)
            for event in _download_chunks(url, temp_name, max_bytes,
                                          DOWNLOAD_DEADLINE,
                                          lambda: _download_cancelled(client_id)):
                if event.get("done"):
                    # Atomically claim completion before publishing the file:
                    # a cancel that won the race must discard, not save.
                    outcome = download_cancels.finish(client_id)
                    if outcome is False:
                        raise DownloadCancelled()
                    entry["size"] = int(event.get("size") or 0)
                    downloads.register(DATA_DIR, entry, temp_name,
                                       DOWNLOAD_MAX_TOTAL, DOWNLOAD_MAX_ITEMS,
                                       identity)
                    saved[0] = True
                    yield json.dumps({"state": "saved",
                                      "item": _download_item(entry)}) + "\n"
                elif event.get("started"):
                    yield json.dumps({
                        "state": "started",
                        "filename": entry["filename"],
                        "title": entry["title"],
                        "total": event.get("total"),
                    }) + "\n"
                else:
                    yield json.dumps({
                        "state": "progress",
                        "received": int(event.get("received") or 0),
                        "total": event.get("total"),
                    }) + "\n"
        except DownloadCancelled:
            yield json.dumps({"state": "cancelled"}) + "\n"
        except DownloadError as exc:
            yield json.dumps({"state": "error", "code": exc.code,
                              "error": exc.message}) + "\n"
        except downloads.StorageError as exc:
            yield json.dumps({"state": "error", "code": 507,
                              "error": str(exc)}) + "\n"
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            app.logger.warning("streaming download failed id=%s for %s: %s",
                               _current_request_id(), _safe_log_url(url), exc)
            yield json.dumps({"state": "error", "code": 502,
                              "error": "could not save that download"}) + "\n"
        finally:
            if client_id:
                download_cancels.finish(client_id)
            if not saved[0]:
                downloads.discard(DATA_DIR, entry, identity)

    return generate()


@app.post("/api/downloads/cancel/<client_id>")
def api_download_cancel(client_id):
    """Cancel a streaming save from any worker (shared SQLite flag)."""
    client_id = _clean_text(client_id, 64)
    if not _DOWNLOAD_CLIENT_RE.match(client_id or ""):
        return _json_error("invalid download id", 400)
    outcome = download_cancels.request_cancel(client_id)
    if outcome == "unknown":
        return _json_error("unknown download", 404)
    if outcome == "finished":
        return _json_error("download is already finished", 409)
    return jsonify({"cancelled": True, "state": "cancelled"})


@app.get("/api/downloads")
def api_downloads():
    """Local Downloads library (server-side copy under RAVEWEB_DATA)."""
    identity = _download_identity()
    if request.args.get("all") == "1" and session.get("role") == "admin":
        entries = downloads.all_entries(DATA_DIR)
    else:
        entries = downloads.list_entries(DATA_DIR, identity)
    page = _page_arg()
    window, page, _pages, total = _slice_page(entries, page)
    items = [_download_item(entry) for entry in window]
    if request.args.get("all") == "1" and session.get("role") == "admin":
        for item, entry in zip(items, window):
            item["identity"] = entry.get("identity") or "owner"
    used = sum(entry["size"] for entry in entries)
    base = "/api/downloads"
    if request.args.get("all") == "1":
        base += "?all=1"
    payload = _paged_payload("Downloads", items, page, total, base)
    payload["storage"] = {
        "used": used,
        "limit": DOWNLOAD_MAX_TOTAL,
        "items": total,
        "max_items": DOWNLOAD_MAX_ITEMS,
    }
    return jsonify(payload)


@app.post("/api/downloads")
def api_download_create():
    """Explicitly save one direct audio file for offline playback."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_error("expected a JSON object", 400)
    source = _clean_text(payload.get("source"), 32).lower()
    url = _clean_text(payload.get("url"), MAX_URL)
    title = _clean_text(payload.get("title"), MAX_TITLE)
    download_url = url
    identity = _download_identity()
    if source in ("ezgarage", "mediafire"):
        # Curated set id only: resolve the Mediafire page here (foreground),
        # never trust a caller-supplied download*.mediafire.com URL.
        set_id = _clean_text(payload.get("source_id") or payload.get("id"), 32).lower()
        if not set_id:
            return _json_error("a curated set id is required", 400)
        try:
            row = ezgarage.entry(set_id)
        except ezgarage.UnknownSet:
            return _json_error("unknown DJ EZ set", 400)
        # Stable, non-expiring identity for the library/dedupe path.  Check it
        # before resolving so an already-saved set costs no Mediafire request.
        url = ezgarage.page_url(set_id)
        existing = downloads.find_url(DATA_DIR, url, identity)
        if existing:
            return jsonify({"saved": True, "already": True,
                            "item": _download_item(existing)})
        try:
            download_url = ezgarage.resolve(set_id)
        except ezgarage.EzGarageError as exc:
            return _ezgarage_error(exc)
        allowed, error = _download_allowed(source, download_url)
        if not allowed:
            return _json_error(error, 400)
        if not title:
            title = row["title"]
    elif (source in ("ukraves", "mikus")
          and (payload.get("source_id") or payload.get("id"))):
        # Resolve server-side from a curated id, never a caller-supplied
        # signed URL.  The stable page URL is the library identity.
        try:
            url = _source_stable_url(source, payload.get("source_id") or payload.get("id"))
        except DownloadError as exc:
            return _json_error(exc.message, exc.code)
        existing = downloads.find_url(DATA_DIR, url, identity)
        if existing:
            return jsonify({"saved": True, "already": True,
                            "item": _download_item(existing)})
        try:
            download_url = _resolve_source_download(source, url)
        except DownloadError as exc:
            return _json_error(exc.message, exc.code)
        allowed, error = _download_allowed(source, download_url)
        if not allowed:
            return _json_error(error, 400)
        if not title:
            title = ("UK Raves mix" if source == "ukraves"
                     else "Mikus Musik MP3")
    else:
        allowed, error = _download_allowed(source, url)
        if not allowed:
            return _json_error(error, 400)
    existing = downloads.find_url(DATA_DIR, url, identity)
    if existing:
        return jsonify({"saved": True, "already": True,
                        "item": _download_item(existing)})
    try:
        downloads.reserve(DATA_DIR, DOWNLOAD_MAX_ITEMS, identity)
    except downloads.StorageError as exc:
        return _json_error(str(exc), 507)
    if not title:
        title = urllib.parse.unquote(url.rsplit("/", 1)[-1]) or "Download"
    entry = downloads.entry_for(DATA_DIR, title, url, source)
    if "application/x-ndjson" in (request.headers.get("Accept") or "").lower():
        client_id = _clean_text(payload.get("client_id"), 64)
        if not _DOWNLOAD_CLIENT_RE.match(client_id or ""):
            return _json_error("invalid download id", 400)
        download_cancels.begin(client_id)
        return Response(
            _download_stream(download_url, entry, client_id, identity),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )
    temp_name = downloads.temp_path(DATA_DIR, entry, identity)
    try:
        used = downloads.used(DATA_DIR, identity)
        max_bytes = min(DOWNLOAD_MAX_BYTES, max(0, DOWNLOAD_MAX_TOTAL - used))
        if max_bytes <= 0:
            raise DownloadError("the Downloads library is full", 413)
        size = _download_to_file(download_url, temp_name, max_bytes, DOWNLOAD_DEADLINE)
        entry["size"] = size
        downloads.register(DATA_DIR, entry, temp_name,
                           DOWNLOAD_MAX_TOTAL, DOWNLOAD_MAX_ITEMS, identity)
    except DownloadError as exc:
        downloads.discard(DATA_DIR, entry, identity)
        return _json_error(exc.message, exc.code)
    except downloads.StorageError as exc:
        downloads.discard(DATA_DIR, entry, identity)
        return _json_error(str(exc), 507)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback
        downloads.discard(DATA_DIR, entry, identity)
        app.logger.warning("download failed id=%s for %s: %s",
                           _current_request_id(), _safe_log_url(url), exc)
        return _json_error("could not save that download", 502)
    return jsonify({"saved": True, "item": _download_item(entry)})


@app.delete("/api/downloads/<download_id>")
def api_download_delete(download_id):
    identifier = _clean_text(download_id, 64)
    identity = _download_identity()
    entry = downloads.find(DATA_DIR, identifier, identity)
    if not entry:
        return _json_error("unknown download", 404)
    try:
        removed = downloads.remove(DATA_DIR, identifier, identity)
    except Exception as exc:  # noqa: BLE001 - a broken store is not a 500
        app.logger.warning("download delete failed: %s", exc)
        removed = False
    if removed:
        try:
            download_jobs.clear_download_reference(identifier, identity)
        except Exception:
            app.logger.exception("jobs: could not clear deleted download reference")
    return jsonify({"removed": bool(removed)})


def _saved_download(download_id):
    identifier = _clean_text(download_id, 64)
    if not identifier:
        return None, None
    identity = _download_identity()
    entry = downloads.find(DATA_DIR, identifier, identity)
    if not entry:
        return None, None
    path = downloads.file_path(DATA_DIR, entry, identity)
    if not os.path.isfile(path):
        return None, None
    # Pre-1.6.65 saves could be registered after an upstream early EOF.  The
    # index records the bytes accepted at save time, so a size mismatch means
    # the local copy is incomplete: never serve it as a complete download.
    expected = int(entry.get("size") or 0)
    try:
        actual = os.path.getsize(path)
    except OSError:
        return None, None
    if expected > 0 and actual != expected:
        app.logger.warning("saved download %s is incomplete (%d of %d bytes)",
                           _safe_log_url(path), actual, expected)
        return entry, None
    return entry, path


def _send_download(entry, path, as_attachment):
    extension = os.path.splitext(entry["filename"])[1].lower()
    mimetype = "audio/mp4" if extension == ".m4a" else "audio/mpeg"
    response = send_file(
        path, mimetype=mimetype, conditional=True, etag=entry["id"],
        max_age=0, as_attachment=as_attachment,
        download_name=entry["filename"] if as_attachment else None,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return response


@app.get("/api/downloads/<download_id>/stream")
def api_download_stream(download_id):
    """Offline playback: the saved copy, Range-capable, same-origin."""
    entry, path = _saved_download(download_id)
    if not entry:
        return _json_error("unknown download", 404)
    if path is None:
        return _incomplete_download_error()
    return _send_download(entry, path, as_attachment=False)


@app.get("/api/downloads/<download_id>/file")
def api_download_file(download_id):
    """Save the local copy to this device, with a safe Content-Disposition name."""
    entry, path = _saved_download(download_id)
    if not entry:
        return _json_error("unknown download", 404)
    if path is None:
        return _incomplete_download_error()
    return _send_download(entry, path, as_attachment=True)


def _incomplete_download_error():
    return _json_error(
        "that saved copy is incomplete — delete it and download it again", 409,
        error_code="download_incomplete")


PRUNE_INTERVAL = 3600
PRUNE_MAX_REMOVALS = 400
PRUNE_MAX_SCAN = 20000
_PRUNE_STATE = {"at": 0.0}


def _prune_cache():
    """Drop stale cache files so a long-lived /data volume cannot fill up.

    The shared cache never deletes expired entries; searches create one file
    per query.  Work per run is bounded (scan and removal counters), and this
    must never raise into a request.
    """
    try:
        directory = os.path.dirname(cache.path_for("prune-probe"))
    except Exception:
        return
    now = time.time()
    limit = now - 14 * 24 * 3600
    removed = 0
    scanned = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        scanned += 1
        if scanned > PRUNE_MAX_SCAN or removed >= PRUNE_MAX_REMOVALS:
            break
        path = os.path.join(directory, name)
        try:
            if name.startswith(".tmp-"):
                if os.path.getmtime(path) < now - 3600:
                    os.remove(path)
                    removed += 1
                continue
            if not re.match(r"^[0-9a-f]{64}\.json$", name):
                continue
            if os.path.getmtime(path) < limit:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        app.logger.info("pruned %d stale cache files from %s", removed, directory)


_PRUNE_LOCK = threading.Lock()


def _prune_cache_worker():
    """Run one bounded prune in a daemon thread; never raise."""
    try:
        _prune_cache()
    except Exception:  # pragma: no cover - maintenance is best effort
        pass
    finally:
        _PRUNE_LOCK.release()


def _maybe_prune_cache():
    """Schedule one _prune_cache per hour, off the request thread.

    It used to run only at worker import; then it ran synchronously on the
    first request of the hour, so a slow /data volume could stall that request
    (including /api/stream) for seconds.  Now the request only checks a clock
    and starts a daemon thread; at most one prune runs at a time.
    """
    now = time.time()
    if now - _PRUNE_STATE["at"] < PRUNE_INTERVAL:
        return
    if not _PRUNE_LOCK.acquire(False):
        return
    _PRUNE_STATE["at"] = now
    try:
        thread = threading.Thread(target=_prune_cache_worker,
                                  name="raveweb-cache-prune", daemon=True)
        thread.start()
    except RuntimeError:  # pragma: no cover - thread creation unavailable
        try:
            _prune_cache()
        finally:
            _PRUNE_LOCK.release()


@app.before_request
def _request_cache_prune():
    try:
        _maybe_prune_cache()
    except Exception:  # maintenance must never break a request
        pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8097")), threaded=True)
