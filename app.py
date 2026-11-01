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

from core import (archive, audit, auth, cache, dnbradio, download_jobs, downloads,
                  favourites, feeds, getdarker, hardscore, latest, legacy, mixcloud,
                  moxser, pirate, radio, raves, ravearchive, soundcloud)

app = Flask(__name__, static_folder="static", template_folder="templates")
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
LOGIN_LIMITER = auth.LoginLimiter(max_failures=AUTH.max_failures,
                                  failure_window=AUTH.failure_window,
                                  lockout_seconds=AUTH.lockout_seconds)
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
DOWNLOAD_SOURCES = ("archive", "moxser", "hardscore", "pirate")
DOWNLOAD_EXTENSIONS = (".mp3", ".m4a")
DOWNLOAD_JOB_CONCURRENCY = max(1, min(2, int(os.environ.get("RAVEWEB_DOWNLOAD_JOBS", "2") or 2)))
DOWNLOAD_JOB_MAX = 20
DOWNLOAD_BLOCKED_HOSTS = (
    "mixcloud.com", "player-widget.mixcloud.com", "soundcloud.com",
    "w.soundcloud.com", "youtube.com", "youtu.be", "vimeo.com",
)
DOWNLOAD_SOURCE_LABELS = {
    "archive": "Internet Archive",
    "moxser": "Moxser",
    "hardscore": "Hardscore",
    "pirate": "Pirate Radio Archives",
}
DOWNLOAD_SOURCE_ART = {
    "archive": ART + "tapepacks.png",
    "moxser": ART + "radio.png",
    "hardscore": ART + "hardcore.png",
    "pirate": ART + "pirate.png",
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
    ("rave", "Rave Tape Packs", "collection:ravetapes AND mediatype:audio", "Rave tapes collection"),
    ("jungle", "Jungle / Drum & Bass", 'collection:ravetapes AND (jungle OR "drum and bass")', "Jungle and DnB"),
    ("hardcore", "Hardcore", "collection:ravetapes AND hardcore", "Hardcore sets"),
    ("house", "House", "collection:ravetapes AND house", "House music sets"),
    ("pirate", "Pirate Radio", 'collection:ravetapes AND (Kool OR Centreforce OR "Don FM")', "Pirate radio sets"),
    ("tapepacks", "All Tape Packs", '(jungle OR "drum and bass" OR hardcore OR house) AND "tape pack"', "Everything tagged tape pack"),
    ("garage", "UK Garage", '(title:("uk garage") OR title:("garage nation") OR title:("pure garage") OR title:("speed garage")) AND mediatype:audio', "UK garage, 90s to now"),
]

RADIO_SECTIONS = [
    ("oldskool", "Oldskool & Rave", "oldskool"),
    ("jungle", "Jungle", "jungle"),
    ("dnb", "Drum & Bass", "drum and bass"),
    ("hardcore", "Hardcore", "hardcore"),
    ("house", "House", "house"),
    ("ukg", "UK Garage", "uk garage"),
    ("breakbeat", "Breakbeat", "breakbeat"),
]

SECTIONS = [
    ("latest", "Latest", "rave.png", "Newest of everything"),
    ("kool", "Kool FM", "radio.png", "Kool FM live plus archive sets"),
    ("rinse", "Rinse FM", "radio.png", "Rinse FM live plus archive sets"),
    ("filters", "Quick Filters", "tapepacks.png", "Classic pack series"),
    ("live", "Live Radio", "radio.png", "Stations streaming now"),
    ("packs", "Tape Packs", "tapepacks.png", "The ravetapes collection"),
    ("podcasts", "Podcasts & Mixes", "jungle.png", "Present-day shows"),
    ("dnb", "DNB Radio", "hardcore.png", "Recent jungle/DnB shows"),
    ("ukg", "UK Garage", "garage.png", "DJ EZ, garage sets, live"),
    ("mixcloud", "Mixcloud Sets", "djez.png", "Live club and rave sets"),
    ("soundcloud", "SoundCloud Sets", "pirate.png", "Curated live sets"),
    ("moxser", "Moxser Mixes", "radio.png", "Underground radio mixes, direct MP3"),
    ("hardscore", "Hardscore", "hardcore.png", "Hardcore/jungle tapes and events"),
    ("ravearchive", "Rave Archive UK", "tapepacks.png", "Rave tape scans with Mixcloud"),
    ("getdarker", "GetDarker", "jungle.png", "Dubstep/grime sets on Mixcloud"),
    ("pirates", "Pirate Radio Archives", "pirate.png", "Curated pirate-radio recordings"),
    ("raves", "Rave Nights", "rave.png", "Promotions, venues & pirate stations"),
    ("years", "By Year", "house.png", "Tape packs 1987 onwards"),
    ("legacy", "Legacy Sites", "pirate.png", "The 2014 survivors"),
    ("favourites", "Favourites", "about.png", "What you saved"),
    ("downloads", "Downloads", "tapepacks.png", "Saved for offline"),
    ("search", "Search", "search.png", "Search the Archive"),
]

# One source of truth for section -> API path; mirrored (and tested) in
# static/app.js so the grouped 8-button rail and the legacy flat list agree.
SECTION_PATHS = {
    "latest": "/api/latest",
    "kool": "/api/filter/kool",
    "rinse": "/api/filter/rinse",
    "filters": "/api/filters",
    "live": "/api/live",
    "packs": "/api/packs",
    "podcasts": "/api/podcasts",
    "dnb": "/api/dnb",
    "ukg": "/api/ukg",
    "mixcloud": "/api/mixcloud_hub",
    "soundcloud": "/api/soundcloud",
    "moxser": "/api/moxser",
    "hardscore": "/api/hardscore",
    "ravearchive": "/api/ravearchive",
    "getdarker": "/api/getdarker",
    "pirates": "/api/pirate",
    "raves": "/api/raves",
    "years": "/api/years",
    "legacy": "/api/legacy",
    "favourites": "/api/favourites",
    "downloads": "/api/downloads",
    "search": "/api/search",
}

# The 8-button rail (1.6.15): it replaces 21 flat buttons, which overflowed a
# 1440x900 viewport and hid Favourites/Search.  Multi-section groups open a
# server-rendered hub (/api/rail/<id>); single sections link straight through.
RAIL = [
    {"id": "latest", "label": "Latest", "icon": "rave.png", "path": "/api/latest",
     "children": ["latest"], "sub": "Newest of everything"},
    {"id": "packs", "label": "Tape Packs", "icon": "tapepacks.png",
     "children": ["packs", "years", "filters"], "sub": "Archive packs, years, classic series"},
    {"id": "shows", "label": "Mixes & Shows", "icon": "radio.png",
     "children": ["podcasts", "dnb", "moxser", "hardscore"], "sub": "Shows, mixes and radio archives"},
    {"id": "sets", "label": "Club & Rave Sets", "icon": "djez.png",
     "children": ["mixcloud", "getdarker", "ravearchive", "ukg", "raves"],
     "sub": "Embedded sets and rave nights"},
    {"id": "soundcloud", "label": "SoundCloud Picks", "icon": "pirate.png",
     "path": "/api/soundcloud", "children": ["soundcloud"], "sub": "Curated live sets"},
    {"id": "live", "label": "Live Radio", "icon": "radio.png",
     "children": ["live", "kool", "rinse"], "sub": "Stations on air now"},
    {"id": "gems", "label": "Curated Gems", "icon": "rave.png",
     "children": ["pirates", "legacy"], "sub": "Pirate archives and surviving sites"},
    {"id": "favourites", "label": "Favourites", "icon": "about.png",
     "path": "/api/favourites", "children": ["favourites"], "sub": "What you saved"},
    {"id": "downloads", "label": "Downloads", "icon": "tapepacks.png",
     "path": "/api/downloads", "children": ["downloads"], "sub": "Saved for offline"},
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
    separator = "&" if "?" in base else "?"
    return "%s%spage=%d" % (base, separator, page + 1)


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


def _json_error(message, code, headers=None):
    response = jsonify({"error": message})
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
        if not sid or not audit.session_active(sid):
            session.clear()
            return False
        if not getattr(g, "_audit_touched", False):
            audit.touch_session(sid, _client_ip(),
                                (request.headers.get("User-Agent") or "")[:300])
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
    "/login", "/logout", "/healthz", "/manifest.webmanifest", "/sw.js",
    "/static/auth.css", "/static/login.js", "/static/sw-register.js",
    "/static/art/icon.png", "/static/art/icon-180.png",
))


def _public_static(path):
    return (path in PUBLIC_PATHS
            or path.startswith("/static/art/icon") and path.endswith(".png"))


@app.before_request
def _auth_guard():
    """Protect the shell, every API route and non-login static assets."""
    path = request.path
    if path.startswith("/api/"):
        if path == "/api/admin/stats" and _widget_authorized():
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
    lock_key = "%s\x00%s" % (ip, (request.form.get("username") or "").strip().lower()[:64] or "-")
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
    identity = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password") or ""
    # Fixed single-operator identities: owner=admin, friends=viewer.  Missing
    # friends hash disables that identity without revealing it on the page.
    credentials = {"owner": ("admin", AUTH.password_hash)}
    if AUTH.friends_hash:
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


@app.after_request
def _record_request_audit(response):
    """Persist endpoint/bytes/identity for the stats feed (best-effort)."""
    try:
        path = request.path
        if (path.startswith("/static/")
                or path in ("/healthz", "/manifest.webmanifest", "/sw.js")):
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
            url = str(entry["url"])
            items.append(_track(title, "%s · %s" % (entry.get("source", ""), stamp), url,
                                ART + "radio.png",
                                {"kind": "stream", "target": url, "title": title}))
        elif kind == "dnb" and entry.get("id") is not None:
            item = _track(title, "%s · %s" % (entry.get("source", ""), stamp), None,
                          ART + "jungle.png")
            item["play_via"] = "/api/dnb/%s" % urllib.parse.quote(str(entry["id"]))
            items.append(item)
    return jsonify({"title": "Latest", "items": items})


@app.get("/api/filters")
def api_filters():
    icons = {"dj-ez": "djez.png", "garage-nation": "garage.png"}
    return jsonify({"title": "Quick Filters", "items": [
        _folder(entry["name"], entry.get("plot", ""),
                "/api/filter/%s" % urllib.parse.quote(entry["id"]),
                ART + icons.get(entry["id"], "tapepacks.png"))
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
        item = _track("▶ %s — live now" % entry["name"], "Live streams", None, ART + "radio.png")
        item["play_via"] = "/api/filter/%s/live" % urllib.parse.quote(key)
        items.append(item)
    docs, total = archive.search(entry["query"], page=page, rows=ROWS)
    items.extend(_pack_items(docs))
    payload = {"title": entry["name"], "items": items}
    nxt = _next_link("/api/filter/%s" % key, page, total)
    if nxt:
        payload["next"] = nxt
    return jsonify(payload)


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
        items.append(_track(name, meta, radio.stream_url(station),
                            station.get("favicon") or ART + "radio.png"))
    return jsonify({"title": "%s — live" % entry["name"], "items": items})


@app.get("/api/live")
def api_live():
    return jsonify({"title": "Live Radio", "items": [
        _folder(label, tag, "/api/live/%s" % key,
                ART + ("garage.png" if key in ("garage", "ukg") else "radio.png"))
        for key, label, tag in RADIO_SECTIONS
    ]})


@app.get("/api/live/<key>")
def api_live_tag(key):
    section = next((entry for entry in RADIO_SECTIONS if entry[0] == key), None)
    if not section:
        return _json_error("unknown station group", 404)
    stations = radio.stations(section[2], limit=100)
    items = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        name, meta = radio.display(station)
        url = radio.stream_url(station)
        if not url:
            continue
        items.append(_track(name, meta, url, station.get("favicon") or ART + "radio.png"))
    return jsonify({"title": section[1], "items": items})


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
                      None, entry.get("thumb") or ART + "radio.png")
        item["embed"] = entry["embed"]
        item["external"] = entry["url"]
        items.append(item)
    return jsonify({"title": "Mixcloud: %s" % query, "items": items})


@app.get("/api/ukg")
def api_ukg():
    """UK garage hub: Mixcloud DJ EZ sets (embeds) plus Archive garage packs."""
    items = []
    for entry in mixcloud.search("DJ EZ", limit=18):
        item = _track(entry["title"], "Mixcloud · %s" % entry["user"], None,
                      entry.get("thumb") or ART + "garage.png")
        item["embed"] = entry["embed"]
        item["external"] = entry["url"]
        items.append(item)
    docs, _ = archive.search(ARCHIVE_SECTIONS[-1][2], rows=30)
    items.extend(_pack_items(docs))
    return jsonify({"title": "UK Garage", "items": items})


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
                ART + "djez.png") for label, query, sub in MIXCLOUD_QUERIES
    ]})


@app.get("/api/soundcloud")
def api_soundcloud():
    query = request.args.get("q", "").strip()
    if query and soundcloud.configured():
        items = []
        for entry in soundcloud.search(query):
            item = _track(entry["title"], "SoundCloud · %s" % entry["user"], None,
                          entry.get("thumb") or ART + "pirate.png")
            item["embed"] = entry["embed"]
            item["external"] = entry["url"]
            items.append(item)
        return jsonify({"title": "SoundCloud: %s" % query, "items": items})
    items = []
    for entry in soundcloud.registry():
        item = _track(entry["title"], "SoundCloud · live set", None, ART + "pirate.png")
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
                             "/api/rave/%s" % entry["slug"], ART + "rave.png"))
    items.sort(key=lambda item: item["label"].lower())
    return jsonify({"title": "Rave Nights", "items": items})


@app.get("/api/rave/<key>")
def api_rave(key):
    entry = raves.entry(key)
    if not entry:
        return jsonify({"error": "unknown rave"}), 404
    items = []
    if entry["mixcloud_query"] and entry["mixcloud_count"]:
        for set_entry in mixcloud.search(entry["mixcloud_query"], limit=12):
            item = _track(set_entry["title"], "Mixcloud · %s" % set_entry["user"], None,
                          set_entry.get("thumb") or ART + "djez.png")
            item["embed"] = set_entry["embed"]
            item["external"] = set_entry["url"]
            items.append(item)
    if entry["archive_query"] and entry["archive_count"]:
        docs, _ = archive.search(entry["archive_query"], rows=30)
        items.extend(_pack_items(docs))
    return jsonify({"title": entry["name"], "items": items})


# --------------------------------------------------------------------------- 1.6.14 mirror sources


@app.get("/api/moxser")
def api_moxser():
    """Moxser mixes (sitemap -> page -> direct MP3, resolved on play)."""
    try:
        entries = moxser.mixes(limit=48)
    except Exception as exc:
        app.logger.warning("moxser list failed: %s", exc)
        return _json_error("could not load Moxser", 502)
    items = []
    for entry in entries:
        title = _clean_text(entry.get("title"), MAX_TITLE)
        slug = entry.get("slug") or ""
        if not title or not slug:
            continue
        item = _track(title, _source_sub("Moxser", entry.get("date")), None,
                      _http_url(entry.get("flyer")) or ART + "radio.png")
        item["play_via"] = "/api/moxser/%s" % urllib.parse.quote(slug, safe="")
        item["download"] = {"source": "moxser"}
        items.append(item)
    return jsonify({"title": "Moxser Mixes", "items": items})


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
        "art": _http_url(mix.get("flyer")) or ART + "radio.png",
    })


@app.get("/api/hardscore")
def api_hardscore():
    """Hardscore mixes with direct audio, plus their event listings."""
    items = []
    for entry in hardscore.mixes(limit=30):
        title = _clean_text(entry.get("title"), MAX_TITLE)
        if not title:
            continue
        sub = _source_sub("Hardscore", entry.get("date"))
        media = _http_url(entry.get("media"))
        if media:
            item = _track(title, sub, media, ART + "hardcore.png")
            _mark_download(item, "hardscore", media)
            items.append(item)
        else:
            link = _http_url(entry.get("link"))
            if link:
                items.append(_external_row(title, sub, link, ART + "hardcore.png"))
    for entry in hardscore.events(limit=10):
        title = _clean_text(entry.get("title"), MAX_TITLE)
        link = _http_url(entry.get("link"))
        if title and link:
            items.append(_external_row(title, _source_sub("Event", entry.get("date")),
                                       link, ART + "hardcore.png"))
    return jsonify({"title": "Hardscore", "items": items})


@app.get("/api/ravearchive")
def api_ravearchive():
    """Rave Archive UK releases (year/DJs from categories), each with a player."""
    items = []
    for entry in ravearchive.releases(limit=30):
        title = _clean_text(entry.get("title"), MAX_TITLE)
        slug = entry.get("slug") or ""
        if not title or not slug:
            continue
        djs = ", ".join(entry.get("djs") or [])[:90]
        items.append(_folder(title, _source_sub(entry.get("year"), djs),
                             "/api/ravearchive/%s" % urllib.parse.quote(slug, safe=""),
                             ART + "tapepacks.png"))
    return jsonify({"title": "Rave Archive UK", "items": items})


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
        item = _track(title, "Mixcloud · Rave Archive UK", None, ART + "tapepacks.png")
        item["embed"] = mixcloud.embed_url(cloudcast)
        item["external"] = cloudcast
        items.append(item)
    elif link:
        items.append(_external_row(title, "Open on Rave Archive UK", link,
                                   ART + "tapepacks.png"))
    if not items:
        return _json_error("no player found for that release", 404)
    return jsonify({"title": title, "items": items})


@app.get("/api/getdarker")
def api_getdarker():
    """GetDarker sets: RSS embeds first, Mixcloud account as fallback."""
    items = []
    for entry in getdarker.sets(limit=30):
        title = _clean_text(entry.get("title"), MAX_TITLE)
        if not title:
            continue
        cloudcast = _http_url(entry.get("mixcloud"))
        if cloudcast:
            item = _track(title, _source_sub("GetDarker", entry.get("date")), None,
                          ART + "jungle.png")
            item["embed"] = mixcloud.embed_url(cloudcast)
            item["external"] = cloudcast
            items.append(item)
            continue
        media = _http_url(entry.get("media"))
        if media:
            items.append(_track(title, _source_sub("GetDarker", entry.get("date")),
                                media, ART + "jungle.png"))
            continue
        link = _http_url(entry.get("link"))
        if link:
            items.append(_external_row(title, _source_sub("GetDarker", entry.get("date")),
                                       link, ART + "jungle.png"))
    return jsonify({"title": "GetDarker", "items": items})


@app.get("/api/pirate")
def api_pirate():
    """Curated Pirate Radio Archives: one folder per station."""
    items = []
    for group in pirate.stations():
        sub = _source_sub("%d archive%s" % (group["count"], "" if group["count"] == 1 else "s"),
                          ", ".join(group.get("years") or []))
        items.append(_folder(group["station"], sub or "curated archive",
                             "/api/pirate/%s" % urllib.parse.quote(group["id"], safe=""),
                             ART + "pirate.png"))
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
            item = _track(title, sub, stream, ART + "pirate.png")
            _mark_download(item, "pirate", stream)
            if source:
                item["external"] = source
        elif source:
            item = _external_row(title, sub, source, ART + "pirate.png")
        else:
            # Kept deliberately: the metadata is the archive, no dead link shown.
            item = _track(title, _source_sub(sub, "link pending"), None, ART + "pirate.png")
        if entry.get("credit"):
            item["credit"] = entry["credit"]
        items.append(item)
    return jsonify({"title": pirate.station(slug)[0]["station"], "items": items})


@app.get("/api/packs")
def api_packs():
    return jsonify({"title": "Tape Packs", "items": [
        _folder(label, sub, "/api/packs/%s" % key, ART + "tapepacks.png")
        for key, label, query, sub in ARCHIVE_SECTIONS
    ]})


@app.get("/api/packs/<key>")
def api_pack_list(key):
    section = next((entry for entry in ARCHIVE_SECTIONS if entry[0] == key), None)
    if not section:
        return _json_error("unknown section", 404)
    page = _page_arg()
    docs, total = archive.search(section[2], page=page, rows=ROWS)
    payload = {"title": section[1], "items": _pack_items(docs)}
    nxt = _next_link("/api/packs/%s" % key, page, total)
    if nxt:
        payload["next"] = nxt
    return jsonify(payload)


@app.get("/api/item/<path:identifier>")
def api_item(identifier):
    identifier = _clean_text(identifier, MAX_QUERY)
    if not identifier or "/" in identifier:
        return _json_error("unknown item", 404)
    tracks = archive.audio_files(identifier)
    if not tracks:
        return _json_error("no playable audio for that item", 404)
    items = []
    for track in tracks:
        item = _track(track["title"], _human_duration(track.get("length")), track["url"],
                      archive.thumb(identifier),
                      {"kind": "stream", "target": track["url"], "title": track["title"]})
        _mark_download(item, "archive", track["url"])
        items.append(item)
    return jsonify({"title": identifier.replace("-", " ").replace("_", " "), "items": items,
                    "art": archive.thumb(identifier)})


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
                ART + "radio.png")
        for entry in entries
    ]
    return jsonify({"title": "Podcasts & Mixes", "items": items})


@app.get("/api/podcast/<feed_id>")
def api_podcast(feed_id):
    entry = feeds.feed(feed_id)
    if not entry:
        return _json_error("unknown feed", 404)
    items = []
    for episode in feeds.episodes(feed_id):
        url = episode.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = _clean_text(episode.get("title"), MAX_TITLE) or "Untitled"
        items.append(_track(title, (episode.get("published") or "")[:16], url, ART + "radio.png",
                            {"kind": "stream", "target": url, "title": title}))
    return jsonify({"title": entry["name"], "items": items})


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
        item = _track(title, _clean_text(show.get("date"), 40), None, ART + "jungle.png")
        item["play_via"] = "/api/dnb/%s" % urllib.parse.quote(str(show["id"]))
        items.append(item)
    return jsonify({"title": "DNB Radio", "items": items})


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
    items = [_folder(str(year), "Tape packs", "/api/year/%d" % year, ART + "tapepacks.png")
             for year in range(this_year, MIN_YEAR - 1, -1)]
    return jsonify({"title": "By Year", "items": items})


@app.get("/api/year/<int:year>")
def api_year(year):
    this_year = datetime.date.today().year
    if year < MIN_YEAR or year > this_year + 1:
        return _json_error("unknown year", 404)
    page = _page_arg()
    docs, total = archive.search(latest.year_query(year), page=page, rows=ROWS)
    payload = {"title": str(year), "items": _pack_items(docs)}
    nxt = _next_link("/api/year/%d" % year, page, total)
    if nxt:
        payload["next"] = nxt
    return jsonify(payload)


@app.get("/api/legacy")
def api_legacy():
    return jsonify({"title": "Legacy Sites", "items": [
        _folder("RaveTapePacks.com", "Full tape packs", "/api/legacy/ravetapepacks", ART + "tapepacks.png"),
        _folder("JungleTapes.com", "Toronto jungle radio shows", "/api/legacy/jungletapes", ART + "jungle.png"),
        _folder("Toronto Rave Mixtape Archive", "1990s mixtapes", "/api/legacy/toronto", ART + "rave.png"),
    ]})


@app.get("/api/legacy/ravetapepacks")
def api_legacy_rtp():
    items = []
    for entry in legacy.packs():
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        tracks = entry.get("tracks") if isinstance(entry.get("tracks"), list) else []
        items.append(_folder("%s (%d tapes)" % (entry.get("name") or entry["key"], len(tracks)),
                             "RaveTapePacks.com",
                             "/api/legacy/ravetapepacks/%s" % urllib.parse.quote(entry["key"])))
    return jsonify({"title": "RaveTapePacks.com", "items": items})


@app.get("/api/legacy/ravetapepacks/<path:pack>")
def api_legacy_rtp_pack(pack):
    entry = next((item for item in legacy.packs()
                  if isinstance(item, dict) and item.get("key") == pack), None)
    if not entry:
        return _json_error("unknown pack", 404)
    items = []
    for track in entry.get("tracks") or []:
        if not isinstance(track, dict) or not track.get("url"):
            continue
        playable = legacy.playable(track["url"])
        name = _clean_text(track.get("name"), MAX_TITLE) or "Tape"
        items.append(_track(name, "Tape", playable, ART + "tapepacks.png",
                            {"kind": "stream", "target": playable, "title": name}))
    return jsonify({"title": entry.get("name") or pack, "items": items})


@app.get("/api/legacy/jungletapes")
def api_legacy_jt():
    items = []
    for show in legacy.jungle_shows():
        if not isinstance(show, dict) or not isinstance(show.get("key"), str):
            continue
        items.append(_folder(show.get("name") or show["key"], "JungleTapes.com",
                             "/api/legacy/jungletapes/%s" % urllib.parse.quote(show["key"])))
    return jsonify({"title": "JungleTapes.com", "items": items})


@app.get("/api/legacy/jungletapes/<path:show_key>")
def api_legacy_jt_show(show_key):
    show_key = _clean_text(show_key, MAX_QUERY)
    if not show_key or "/" in show_key:
        return _json_error("unknown show", 404)
    items = []
    for track in legacy.jungle_tracks(show_key):
        if not isinstance(track, dict) or not track.get("url"):
            continue
        name = _clean_text(track.get("name"), MAX_TITLE) or "Track"
        items.append(_track(name, "JungleTapes.com", track["url"], ART + "jungle.png",
                            {"kind": "stream", "target": track["url"], "title": name}))
    if not items:
        return _json_error("no recordings for that show", 404)
    return jsonify({"title": show_key.replace("-", " ").replace("_", " "), "items": items})


@app.get("/api/legacy/toronto")
def api_legacy_toronto():
    items = []
    for track in legacy.toronto_tracks():
        if not isinstance(track, dict) or not track.get("url"):
            continue
        name = _clean_text(track.get("name"), MAX_TITLE) or "Track"
        items.append(_track(name, "Toronto Rave Mixtape Archive", track["url"], ART + "rave.png",
                            {"kind": "stream", "target": track["url"], "title": name}))
    return jsonify({"title": "Toronto Rave Mixtape Archive", "items": items})


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
                        ART + "pirate.png")


def _search_mixcloud(query):
    return _embed_items(mixcloud.search(query, limit=12), "Mixcloud",
                        ART + "djez.png")


@app.get("/api/search")
def api_search():
    query = _clean_text(request.args.get("q", ""), MAX_QUERY)
    if not query:
        return jsonify({"title": "Search", "items": []})
    page = _page_arg()
    docs, total = archive.search("%s AND %s" % (query, latest.RAVE_COLLECTION),
                                 page=page, rows=ROWS)
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
        calls.append(lambda: archive.search("%s AND mediatype:audio" % query,
                                            rows=ROWS)[0])
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
    payload = {"title": "Search: %s" % query, "items": items}
    nxt = None if widen_needed else _next_link(
        "/api/search?q=%s" % urllib.parse.quote(query), page, total)
    if nxt:
        payload["next"] = nxt
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
    try:
        entries = favourites.load()
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
        else:
            item = _track(title, "Show", target, ART + "radio.png")
        item["fav_key"] = key
        items.append(item)
    return jsonify({"title": "Favourites", "items": items})


def _fav_payload():
    """Validate a favourites POST body; returns (entry, error)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, "expected a JSON object"
    kind = payload.get("kind", "pack")
    if kind not in ("pack", "stream"):
        return None, "kind must be pack or stream"
    title = _clean_text(payload.get("title"), MAX_TITLE)
    target = _clean_text(payload.get("target"), MAX_URL)
    if not title:
        return None, "title is required"
    if not target:
        return None, "target is required"
    if kind == "stream":
        try:
            base = _split_kodi_target(target)[0]
            parsed = urllib.parse.urlsplit(base)
        except ValueError:
            return None, "stream target must be an http(s) url"
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None, "stream target must be an http(s) url"
    return {"kind": kind, "target": target, "title": title}, None


@app.post("/api/favourites")
def api_fav_add():
    entry, error = _fav_payload()
    if error:
        return jsonify({"saved": False, "error": error}), 400
    try:
        saved = favourites.add(entry)
    except Exception as exc:  # read-only or full data dir
        app.logger.warning("favourites add failed: %s", exc)
        saved = False
    return jsonify({"saved": bool(saved)})


@app.delete("/api/favourites/<path:key>")
def api_fav_remove(key):
    key = _clean_text(key, MAX_URL)
    try:
        existed = any(favourites.key(entry) == key for entry in favourites.load()
                      if isinstance(entry, dict))
        removed = favourites.remove(key) if existed else False
    except Exception as exc:
        app.logger.warning("favourites remove failed: %s", exc)
        existed, removed = False, False
    return jsonify({"removed": bool(existed and removed)})


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


def _open_stream(url, headers, deadline):
    """GET url, following public redirects by hand.  Returns (conn, resp, url)."""
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
        conn = _new_connection(scheme, host, port, ip)
        try:
            conn.request("GET", _request_target(current), headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError):
            conn.close()
            raise UpstreamTimeout()
        except (http.client.HTTPException, ssl.SSLError, OSError, ValueError) as exc:
            conn.close()
            app.logger.info("upstream connect failed for %s: %s", _safe_log_url(current), exc)
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


def _stream_error(code, message, retry=None):
    headers = {}
    if retry is not None:
        headers["Retry-After"] = str(retry)
    return _json_error(message, code, headers)


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

    if not _STREAM_SLOTS.acquire(timeout=2.0):
        return _stream_error(503, "too many streams in progress, retry shortly", retry=2)

    released = [False]

    def release():
        if not released[0]:
            released[0] = True
            try:
                _STREAM_SLOTS.release()
            except ValueError:  # pragma: no cover - double release guard
                pass

    try:
        headers = _upstream_headers(target, options, request.headers.get("Range"),
                                    request.args.get("ref"))
        deadline = time.monotonic() + STREAM_DEADLINE
        conn, resp, final_url = _open_stream(target, headers, deadline)
    except BlockedURL:
        release()
        return _stream_error(400, "blocked or missing url")
    except UpstreamTimeout:
        release()
        return _stream_error(504, "upstream timed out")
    except UpstreamError:
        release()
        return _stream_error(502, "upstream connection failed")
    except Exception as exc:  # never leak a traceback to the client
        release()
        app.logger.warning("stream setup failed for %s: %s", _safe_log_url(target), exc)
        return _stream_error(502, "upstream connection failed")

    status = resp.status
    try:
        if status in (401, 403):
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
            app.logger.info("stream refused non-media content-type %r for %s",
                            content_type, _safe_log_url(final_url))
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


def _close_and_error(conn, resp, release, code, message, headers_out=None):
    _close_quietly(conn, resp, release)
    response = _json_error(message, code)
    for name, value in (headers_out or {}).items():
        response.headers[name] = value
    return response


# --------------------------------------------------------------------------- pages


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True,
                    "version": APP_VERSION,
                    "data_writable": _data_writable()})


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
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if (request.path in ("/", "/login", "/logout")
            or request.path.startswith("/static/")):
        response.headers.setdefault("Content-Security-Policy", CSP)
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
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


_SHORT_ERRORS = {
    400: "bad request",
    404: "not found",
    405: "method not allowed",
    413: "request body too large",
    500: "internal error",
}


@app.errorhandler(HTTPException)
def _http_error(exc):
    code = exc.code or 500
    if request.path.startswith("/api/"):
        if code == 500:
            app.logger.error("unhandled error on %s", request.path,
                             exc_info=getattr(exc, "original_exception", None))
        return _json_error(_SHORT_ERRORS.get(code, exc.description or "error"), code)
    if code == 500:
        app.logger.error("unhandled error on %s", request.path,
                         exc_info=getattr(exc, "original_exception", None))
        return "Internal Server Error", 500
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


_DOWNLOAD_CANCELS = set()
_DOWNLOAD_CANCEL_LOCK = threading.Lock()
_DOWNLOAD_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _download_cancelled(client_id):
    if not client_id:
        return False
    with _DOWNLOAD_CANCEL_LOCK:
        return client_id in _DOWNLOAD_CANCELS


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
    """The single direct-file eligibility rule (shared with the Kodi add-on)."""
    if source not in DOWNLOAD_SOURCES:
        return False, "that source cannot be downloaded"
    if not isinstance(url, str) or len(url) > MAX_URL:
        return False, "blocked or missing url"
    try:
        parts = urllib.parse.urlsplit(url)
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
    if not parts.path.lower().endswith(DOWNLOAD_EXTENSIONS):
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
        if url not in known:
            return False, "not a curated pirate archive file"
    return True, ""


def _mark_download(item, source, url=None):
    """Expose the Download action only for eligible rows from the four sources."""
    if url is not None and not _download_allowed(source, url)[0]:
        return item
    item["download"] = {"source": source}
    return item


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
                  DOWNLOAD_SOURCE_ART.get(entry.get("source"), ART + "tapepacks.png"))
    item["download_key"] = identifier
    item["file"] = "/api/downloads/%s/file" % urllib.parse.quote(identifier, safe="")
    return item


def _download_chunks(url, destination, max_bytes, seconds, should_cancel=None):
    """Proxy one direct audio file, yielding progress dicts.

    Reuses ``_open_stream`` so the same SSRF, DNS-pinning and redirect checks as
    /api/stream apply; every byte counts against the per-file and library caps.
    Yields ``{"started": True, "total": N|None}`` after the headers, then
    ``{"received": n, "total": ...}`` roughly ten times a second, then
    ``{"done": True, "size": n}``.  The caller owns cleanup of ``destination``.
    """
    headers = _upstream_headers(url, {}, None, None)
    deadline = time.monotonic() + seconds
    conn = resp = handle = None
    try:
        conn, resp, final_url = _open_stream(url, headers, deadline)
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
        handle = open(destination, "wb")
        while True:
            if should_cancel is not None and should_cancel():
                raise DownloadCancelled()
            if time.monotonic() >= deadline:
                raise DownloadError("the download timed out", 504)
            try:
                chunk = resp.read(64 * 1024)
            except (socket.timeout, TimeoutError):
                raise DownloadError("the download timed out", 504)
            if not chunk:
                break
            total += len(chunk)
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
_DOWNLOAD_JOB_STATE = {"base": None, "jobs": {}}
_DOWNLOAD_JOB_SLOTS = threading.BoundedSemaphore(DOWNLOAD_JOB_CONCURRENCY)


def _jobs_ensure():
    base = DATA_DIR
    with _DOWNLOAD_JOBS_LOCK:
        if _DOWNLOAD_JOB_STATE["base"] == base:
            return
        recovered = download_jobs.recover(base)
        _DOWNLOAD_JOB_STATE["base"] = base
        _DOWNLOAD_JOB_STATE["jobs"] = {job["id"]: job for job in recovered}


def _jobs_save_locked():
    download_jobs.save(DATA_DIR, list(_DOWNLOAD_JOB_STATE["jobs"].values()))


def _job_get(job_id):
    _jobs_ensure()
    with _DOWNLOAD_JOBS_LOCK:
        return _DOWNLOAD_JOB_STATE["jobs"].get(job_id)


def _job_visible(job):
    if not job:
        return False
    return (session.get("role") == "admin"
            or job.get("identity") == session.get("identity"))


def _job_update(job_id, save=True, **fields):
    with _DOWNLOAD_JOBS_LOCK:
        job = _DOWNLOAD_JOB_STATE["jobs"].get(job_id)
        if job is None:
            return None
        job.update(fields)
        job["updated"] = time.time()
        if save:
            _jobs_save_locked()
        return job


def _job_cancel_requested(job_id):
    job = _job_get(job_id)
    return bool(job and (job.get("cancel_requested")
                         or job.get("state") in ("cancelled",)))


def _job_cleanup_partial(job):
    if not job:
        return
    try:
        os.remove(download_jobs.temp_path(DATA_DIR, job))
    except OSError:
        pass


def _job_worker(job_id):
    with _DOWNLOAD_JOB_SLOTS:
        job = _job_get(job_id)
        if not job or job.get("state") != "queued" or job.get("cancel_requested"):
            if job and job.get("cancel_requested"):
                _job_update(job_id, state="cancelled", finished=time.time())
                _job_cleanup_partial(job)
            return
        entry = job["entry"]
        temp_name = download_jobs.temp_path(DATA_DIR, job)
        _job_update(job_id, state="downloading", started=time.time(), progress=0,
                    bytes_done=0, bytes_total=None, error="")
        completed = False
        try:
            used = downloads.used(DATA_DIR)
            max_bytes = min(DOWNLOAD_MAX_BYTES, max(0, DOWNLOAD_MAX_TOTAL - used))
            if max_bytes <= 0:
                raise DownloadError("the Downloads library is full", 413)
            last_save = time.monotonic()
            for event in _download_chunks(
                    entry["url"], temp_name, max_bytes, DOWNLOAD_DEADLINE,
                    lambda: _job_cancel_requested(job_id)):
                if _job_cancel_requested(job_id):
                    raise DownloadCancelled()
                if event.get("started"):
                    _job_update(job_id, bytes_total=event.get("total"),
                                progress=0)
                elif event.get("received") is not None:
                    total = event.get("total")
                    done = max(0, int(event.get("received") or 0))
                    percent = int(done * 100 / total) if total else 0
                    now = time.monotonic()
                    _job_update(job_id, save=(now - last_save >= 0.5),
                                bytes_done=done, bytes_total=total,
                                progress=max(0, min(100, percent)))
                    if now - last_save >= 0.5:
                        last_save = now
                elif event.get("done"):
                    entry["size"] = int(event.get("size") or 0)
                    downloads.register(DATA_DIR, entry, temp_name,
                                       DOWNLOAD_MAX_TOTAL, DOWNLOAD_MAX_ITEMS)
                    _job_update(job_id, state="completed",
                                bytes_done=entry["size"],
                                bytes_total=entry["size"], progress=100,
                                finished=time.time(),
                                download_id=entry["id"], error="")
                    completed = True
        except DownloadCancelled:
            _job_update(job_id, state="cancelled", finished=time.time(),
                        error="cancelled")
        except DownloadError as exc:
            _job_update(job_id, state="failed", finished=time.time(),
                        error=exc.message)
        except downloads.StorageError as exc:
            _job_update(job_id, state="failed", finished=time.time(),
                        error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never kill the worker silently
            app.logger.warning("background download %s failed: %s",
                               job_id, exc)
            _job_update(job_id, state="failed", finished=time.time(),
                        error="could not save that download")
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
    allowed, error = _download_allowed(source, url)
    if not allowed:
        return _json_error(error, 400)
    identity = session.get("identity") or ""
    role = session.get("role") or ""
    existing = downloads.find_url(DATA_DIR, url)
    if existing:
        return jsonify({"id": existing["id"], "already": True,
                        "state": "completed", "job": None})
    with _DOWNLOAD_JOBS_LOCK:
        active = [job for job in _DOWNLOAD_JOB_STATE["jobs"].values()
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
        downloads.reserve(DATA_DIR, DOWNLOAD_MAX_ITEMS)
    except downloads.StorageError as exc:
        return _json_error(str(exc), 507)
    if not title:
        title = urllib.parse.unquote(url.rsplit("/", 1)[-1]) or "Download"
    entry = downloads.entry_for(DATA_DIR, title, url, source)
    job = download_jobs.new(identity, role, title, source, url, entry)
    with _DOWNLOAD_JOBS_LOCK:
        _DOWNLOAD_JOB_STATE["jobs"][job["id"]] = job
        _jobs_save_locked()
    threading.Thread(target=_job_worker, args=(job["id"],),
                     name="download-job-%s" % job["id"][:8],
                     daemon=True).start()
    return jsonify({"id": job["id"], "job": download_jobs.public(job)}), 202


@app.get("/api/downloads/jobs")
def api_download_jobs():
    _jobs_ensure()
    with _DOWNLOAD_JOBS_LOCK:
        jobs = [download_jobs.public(job)
                for job in _DOWNLOAD_JOB_STATE["jobs"].values()
                if _job_visible(job)]
    jobs.sort(key=lambda item: item.get("created") or 0, reverse=True)
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
    _job_update(identifier, state="cancelled", cancel_requested=True,
                finished=time.time(), error="cancelled")
    _job_cleanup_partial(job)
    return jsonify({"id": identifier, "state": "cancelled",
                    "job": download_jobs.public(_job_get(identifier))})


def _download_stream(url, entry, client_id):
    """NDJSON progress stream for one explicit download (panel wiring).

    Emits ``started`` (with Content-Length when known), ``progress`` ticks,
    then exactly one of ``saved``/``cancelled``/``error``.  The partial file is
    discarded on any non-saved exit, including client disconnects.
    """
    temp_name = downloads.temp_path(DATA_DIR, entry)
    saved = [False]

    def generate():
        try:
            used = downloads.used(DATA_DIR)
            max_bytes = min(DOWNLOAD_MAX_BYTES, max(0, DOWNLOAD_MAX_TOTAL - used))
            if max_bytes <= 0:
                raise DownloadError("the Downloads library is full", 413)
            for event in _download_chunks(url, temp_name, max_bytes,
                                          DOWNLOAD_DEADLINE,
                                          lambda: _download_cancelled(client_id)):
                if event.get("done"):
                    entry["size"] = int(event.get("size") or 0)
                    downloads.register(DATA_DIR, entry, temp_name,
                                       DOWNLOAD_MAX_TOTAL, DOWNLOAD_MAX_ITEMS)
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
            app.logger.warning("streaming download failed for %s: %s",
                               _safe_log_url(url), exc)
            yield json.dumps({"state": "error", "code": 502,
                              "error": "could not save that download"}) + "\n"
        finally:
            if client_id:
                with _DOWNLOAD_CANCEL_LOCK:
                    _DOWNLOAD_CANCELS.discard(client_id)
            if not saved[0]:
                downloads.discard(DATA_DIR, entry)

    return generate()


@app.post("/api/downloads/cancel/<client_id>")
def api_download_cancel(client_id):
    """Best-effort cancel for the panel: the streaming loop checks this set."""
    client_id = _clean_text(client_id, 64)
    if not _DOWNLOAD_CLIENT_RE.match(client_id or ""):
        return _json_error("invalid download id", 400)
    with _DOWNLOAD_CANCEL_LOCK:
        _DOWNLOAD_CANCELS.add(client_id)
    return jsonify({"cancelled": True})


@app.get("/api/downloads")
def api_downloads():
    """Local Downloads library (server-side copy under RAVEWEB_DATA)."""
    entries = downloads.list_entries(DATA_DIR)
    items = [_download_item(entry) for entry in entries]
    used = sum(entry["size"] for entry in entries)
    return jsonify({
        "title": "Downloads",
        "items": items,
        "storage": {
            "used": used,
            "limit": DOWNLOAD_MAX_TOTAL,
            "items": len(items),
            "max_items": DOWNLOAD_MAX_ITEMS,
        },
    })


@app.post("/api/downloads")
def api_download_create():
    """Explicitly save one direct audio file for offline playback."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_error("expected a JSON object", 400)
    source = _clean_text(payload.get("source"), 32).lower()
    url = _clean_text(payload.get("url"), MAX_URL)
    title = _clean_text(payload.get("title"), MAX_TITLE)
    allowed, error = _download_allowed(source, url)
    if not allowed:
        return _json_error(error, 400)
    existing = downloads.find_url(DATA_DIR, url)
    if existing:
        return jsonify({"saved": True, "already": True,
                        "item": _download_item(existing)})
    try:
        downloads.reserve(DATA_DIR, DOWNLOAD_MAX_ITEMS)
    except downloads.StorageError as exc:
        return _json_error(str(exc), 507)
    if not title:
        title = urllib.parse.unquote(url.rsplit("/", 1)[-1]) or "Download"
    entry = downloads.entry_for(DATA_DIR, title, url, source)
    if "application/x-ndjson" in (request.headers.get("Accept") or "").lower():
        client_id = _clean_text(payload.get("client_id"), 64)
        if not _DOWNLOAD_CLIENT_RE.match(client_id or ""):
            return _json_error("invalid download id", 400)
        return Response(
            _download_stream(url, entry, client_id),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )
    temp_name = downloads.temp_path(DATA_DIR, entry)
    try:
        used = downloads.used(DATA_DIR)
        max_bytes = min(DOWNLOAD_MAX_BYTES, max(0, DOWNLOAD_MAX_TOTAL - used))
        if max_bytes <= 0:
            raise DownloadError("the Downloads library is full", 413)
        size = _download_to_file(url, temp_name, max_bytes, DOWNLOAD_DEADLINE)
        entry["size"] = size
        downloads.register(DATA_DIR, entry, temp_name,
                           DOWNLOAD_MAX_TOTAL, DOWNLOAD_MAX_ITEMS)
    except DownloadError as exc:
        downloads.discard(DATA_DIR, entry)
        return _json_error(exc.message, exc.code)
    except downloads.StorageError as exc:
        downloads.discard(DATA_DIR, entry)
        return _json_error(str(exc), 507)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback
        downloads.discard(DATA_DIR, entry)
        app.logger.warning("download failed for %s: %s", _safe_log_url(url), exc)
        return _json_error("could not save that download", 502)
    return jsonify({"saved": True, "item": _download_item(entry)})


@app.delete("/api/downloads/<download_id>")
def api_download_delete(download_id):
    identifier = _clean_text(download_id, 64)
    try:
        removed = downloads.remove(DATA_DIR, identifier)
    except Exception as exc:  # noqa: BLE001 - a broken store is not a 500
        app.logger.warning("download delete failed: %s", exc)
        removed = False
    return jsonify({"removed": bool(removed)})


def _saved_download(download_id):
    identifier = _clean_text(download_id, 64)
    if not identifier:
        return None, None
    entry = downloads.find(DATA_DIR, identifier)
    if not entry:
        return None, None
    path = downloads.file_path(DATA_DIR, entry)
    if not os.path.isfile(path):
        return None, None
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
    return _send_download(entry, path, as_attachment=False)


@app.get("/api/downloads/<download_id>/file")
def api_download_file(download_id):
    """Save the local copy to this device, with a safe Content-Disposition name."""
    entry, path = _saved_download(download_id)
    if not entry:
        return _json_error("unknown download", 404)
    return _send_download(entry, path, as_attachment=True)


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
