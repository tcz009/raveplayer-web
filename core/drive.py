# -*- coding: utf-8 -*-
"""Google Drive file-id resolver for the Mikus Musik blog.

A Drive share link only carries a file id.  ``uc?export=download&id=...``
leads to a signed ``drive.usercontent.google.com`` URL; for files over ~100 MB
Google interposes a "can't scan this file for viruses" HTML page with a
confirm form, so the resolver parses that form and follows its hidden fields.
A ``Range: bytes=0-2047`` probe identifies the real media type without pulling
a 100 MB file, and the final URL is cached for two hours.

Only Drive hosts are ever contacted; the app's download allowlist repeats the
same host set.
"""

import html
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UC_URL = "https://drive.google.com/uc?export=download&id=%s"
VIEW_URL = "https://drive.google.com/file/d/%s/view"
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
PATH_ID_RE = re.compile(r"/file/d/([A-Za-z0-9_-]{10,100})")
QUERY_ID_RE = re.compile(r"(?:^|[?&])id=([A-Za-z0-9_-]{10,100})(?:&|$)")
FORM_RE = re.compile(
    r"<form[^>]*id=\"download-form\"[^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL)
ACTION_RE = re.compile(r"<form[^>]*\baction=\"([^\"]+)\"", re.IGNORECASE)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"\b(name|value|type)\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TTL = 2 * 60 * 60
NEGATIVE_TTL = 5 * 60
TIMEOUT = 25
MAX_PROBE_BYTES = 4096
AUDIO_EXTENSIONS = (".mp3", ".m4a")

_LOCK = threading.RLock()
_CACHE = {}
_CLOCK = time.monotonic


class DriveError(Exception):
    """A Drive file could not be resolved to a direct audio URL."""

    code = 502

    def __init__(self, message, code=None):
        super(DriveError, self).__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = int(code)


class UnknownDriveFile(DriveError):
    code = 404


class DriveNotAudio(DriveError):
    code = 404


def clear_memory():
    with _LOCK:
        _CACHE.clear()


def file_id(value):
    """Return the Drive file id in a URL/id, or ``""``.

    Accepts a bare id, ``/file/d/<id>/view``, ``uc?id=<id>`` and the
    ``open?id=<id>`` form used by older Blogger posts.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if FILE_ID_RE.match(text):
        return text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    match = PATH_ID_RE.search(parts.path or "")
    if match:
        return match.group(1)
    match = QUERY_ID_RE.search(parts.query or "")
    if match:
        return match.group(1)
    return ""


def view_url(value):
    wanted = file_id(value)
    if not wanted:
        raise UnknownDriveFile("unknown Google Drive file")
    return VIEW_URL % urllib.parse.quote(wanted, safe="")


def _attr(tag, name):
    for match in ATTR_RE.finditer(tag or ""):
        if match.group(1).lower() == name:
            return match.group(2)
    return ""


def parse_confirm_form(body):
    """Build the confirmed download URL from Drive's virus-scan page."""
    if not isinstance(body, str) or not body:
        return ""
    form = FORM_RE.search(body)
    if not form:
        return ""
    block = form.group(0)
    action = ACTION_RE.search(block)
    if not action:
        return ""
    params = {}
    for tag in INPUT_RE.findall(block):
        name = _attr(tag, "name")
        kind = (_attr(tag, "type") or "text").lower()
        if not name or kind == "submit":
            continue
        params[name] = _attr(tag, "value")
    if not params:
        return ""
    action_url = action.group(1)
    try:
        parts = urllib.parse.urlsplit(action_url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not (host == "google.com"
                                       or host.endswith(".google.com")):
        return ""
    return action_url + "?" + urllib.parse.urlencode(params)


def _cache_get(key):
    record = _CACHE.get(key)
    if not record:
        return None
    if record.get("expires", 0.0) <= _CLOCK():
        _CACHE.pop(key, None)
        return None
    return record


def _remember(key, value):
    _CACHE[key] = {"value": value, "expires": _CLOCK() + TTL}


def _remember_error(key, error):
    _CACHE[key] = {"error": error, "expires": _CLOCK() + NEGATIVE_TTL}


def _probe(url):
    """Fetch at most a few KB and return ``(status, headers, final_url, body)``.

    Tests replace this function.  Range keeps the probe cheap even when the
    target turns out to be a 100 MB MP3.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-%d" % (MAX_PROBE_BYTES - 1),
    })
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code or 0), headers, str(exc.geturl() or url), b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DriveError("Google Drive could not be reached (%s)"
                         % type(exc).__name__)
    try:
        status = int(getattr(response, "status", 200) or 200)
        headers = dict(response.headers.items()) if response.headers else {}
        final_url = str(response.geturl() or url)
        body = response.read(MAX_PROBE_BYTES)
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass
    return status, headers, final_url, body


def _header(headers, name):
    wanted = (name or "").lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def _is_audio(headers):
    content_type = _header(headers, "content-type").split(";")[0].strip().lower()
    if content_type.startswith("audio/"):
        return True
    disposition = _header(headers, "content-disposition").lower()
    return any(disposition.rstrip("\"").endswith(ext) for ext in AUDIO_EXTENSIONS)


def _resolve_unlocked(wanted):
    status, headers, final_url, body = _probe(UC_URL % urllib.parse.quote(wanted, safe=""))
    if status in (401, 403):
        raise DriveError("Google Drive refused access to that file")
    if status in (404, 410):
        raise UnknownDriveFile("that Google Drive file is no longer available")
    if status < 200 or status >= 400:
        raise DriveError("Google Drive returned HTTP %s" % status)
    if _is_audio(headers):
        return final_url
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    confirm = parse_confirm_form(text)
    if not confirm:
        raise DriveNotAudio("that Google Drive file is not a playable audio file")
    status, headers, final_url, _body = _probe(confirm)
    if status < 200 or status >= 400:
        raise DriveNotAudio("that Google Drive file is not a playable audio file")
    if not _is_audio(headers):
        raise DriveNotAudio("that Google Drive file is not a playable audio file")
    return final_url


def resolve(file_id_value):
    """Return a direct, short-lived audio URL for a Drive file id.

    Raises :class:`UnknownDriveFile`, :class:`DriveNotAudio` or
    :class:`DriveError`.  Successful URLs are cached for :data:`TTL`,
    failures briefly.
    """
    wanted = file_id(file_id_value)
    if not wanted:
        raise UnknownDriveFile("unknown Google Drive file")
    with _LOCK:
        record = _cache_get(wanted)
        if record is not None:
            if record.get("error") is not None:
                error = record["error"]
                raise type(error)(error.message, code=error.code)
            return record["value"]
        try:
            url = _resolve_unlocked(wanted)
        except DriveError as exc:
            _remember_error(wanted, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - keep resolver failures typed
            error = DriveError("Google Drive resolve failed (%s)"
                               % type(exc).__name__)
            _remember_error(wanted, error)
            raise error
        _remember(wanted, url)
        return url
