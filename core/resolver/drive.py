# -*- coding: utf-8 -*-
"""Google Drive adapter for the resolver service.

Drive's ``uc?export=download`` endpoint usually answers with a short-lived
signed ``drive.usercontent.google.com`` URL, or with an HTML "can't scan this
file" confirm form for large files.  The adapter fetches both pages through
the **injected, bounded** HTTP client (:class:`base.UrllibHTTPClient`), reusing
:mod:`core.drive`'s file-id and confirm-form parsers.  It never calls
``core.drive._probe``/``core.drive.resolve``: those use ``urlopen`` with a
per-recv timeout, follow redirects blindly and hold one process-global lock.

Redirect policy: a 3xx is never followed blindly.  Only a ``Location`` on an
allowlisted Drive host is followed, up to :data:`MAX_REDIRECTS`, and every hop
is re-normalised and re-checked against the SSRF policy.  A redirect to a
loopback/LAN/unlisted host is refused with ``502 upstream`` and no second
request is made.
"""

import socket
import time
import urllib.parse

from . import base

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TTL = 2 * 60 * 60
MAX_PROBE_BYTES = 4096
MAX_REDIRECTS = 3
HOSTS = ("drive.google.com", "docs.google.com")
ALLOWLIST = base.HostAllowlist(
    HOSTS + ("drive.usercontent.google.com",))

PROBE_HEADERS = base.sanitize_headers({
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Range": "bytes=0-%d" % (MAX_PROBE_BYTES - 1),
})


def _host(url):
    try:
        return (base.url_host(url) or "").lower().rstrip(".")
    except Exception:
        return ""


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
    return any(disposition.rstrip("\"").endswith(ext)
               for ext in (".mp3", ".m4a"))


class DriveResolver(object):
    """Resolve a Drive file id/page to a short-lived direct audio URL."""

    source = "drive"
    allowlist = ALLOWLIST
    headers = {"User-Agent": UA}

    def __init__(self, timeout=None, http=None, dns=None, clock=None):
        self.timeout = base.clamp_timeout(timeout)
        self.http = http
        self.dns = dns
        self.clock = clock if callable(clock) else time.time

    def matches(self, url):
        """Cheap: exact Drive host plus a file id; no I/O."""
        try:
            parts = base.split_url(url)
        except base.ResolverError:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        if host not in HOSTS:
            return False
        return bool(parts.path and "/file/d/" in parts.path) or bool(parts.query
                                                                      and "id=" in parts.query)

    def resolve(self, url):
        from core import drive as drive_parse
        if not self.matches(url):
            raise base.UnknownURL("not a Google Drive file")
        wanted = drive_parse.file_id(url)
        if not wanted:
            raise base.NotFound("unknown Google Drive file")

        uc_url = drive_parse.UC_URL % urllib.parse.quote(wanted, safe="")
        status, headers, final_url, body = self._fetch(uc_url)
        if status in (401, 403):
            raise base.UpstreamError("Google Drive refused access to that file")
        if status in (404, 410):
            raise base.NotFound("that Google Drive file is no longer available")
        if status < 200 or status >= 400:
            raise base.UpstreamError("Google Drive returned HTTP %d" % status)
        if _is_audio(headers):
            return self._result(final_url)

        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
        confirm = drive_parse.parse_confirm_form(text)
        if not confirm:
            raise base.NotFound(
                "that Google Drive file is not a playable audio file")
        status, headers, final_url, _body = self._fetch(confirm)
        if status < 200 or status >= 400:
            raise base.NotFound(
                "that Google Drive file is not a playable audio file")
        if not _is_audio(headers):
            raise base.NotFound(
                "that Google Drive file is not a playable audio file")
        return self._result(final_url)

    def _result(self, final_url):
        try:
            normalized = base.normalize_url(final_url)
        except base.ResolverError:
            raise base.UpstreamError("Google Drive returned an invalid URL")
        if not self.allowlist.allows(_host(normalized)):
            raise base.UpstreamError(
                "Google Drive returned a URL outside its download hosts")
        return base.Resolved(
            normalized, dict(self.headers), self.clock() + TTL, self.source)

    # -- bounded fetch with allowlisted hops --------------------------------

    def _fetch(self, url):
        """GET *url*, following only allowlisted Drive redirects.

        Returns ``(status, headers, final_url, body)``.  The HTTP client owns
        the wall-clock deadline and body cap; this loop owns the redirect
        policy and revalidates every hop against the provider allowlist and
        the SSRF guard.
        """
        if self.http is None:
            raise base.UpstreamError("Drive resolver has no HTTP client")
        target = base.normalize_url(url)
        for hop in range(MAX_REDIRECTS + 1):
            if not self.allowlist.allows(_host(target)):
                raise base.UpstreamError(
                    "Google Drive redirect target is outside its download hosts")
            base.assert_public_url(target, dns=self.dns)
            try:
                response = self.http.request(
                    "GET", target, headers=dict(PROBE_HEADERS),
                    timeout=self.timeout)
            except base.ResolverError:
                raise
            except (socket.timeout, TimeoutError):
                raise base.UpstreamTimeout("Google Drive request timed out")
            except OSError:
                raise base.UpstreamError("Google Drive could not be reached")
            except Exception:
                raise base.UpstreamError("Google Drive request failed")

            status = int(getattr(response, "status", 0) or 0)
            headers = dict(getattr(response, "headers", {}) or {})
            body = getattr(response, "body", b"") or b""
            if 300 <= status < 400:
                location = _header(headers, "location").strip()
                if not location:
                    raise base.UpstreamError("Google Drive redirect has no target")
                if hop >= MAX_REDIRECTS:
                    raise base.UpstreamError("Google Drive returned too many redirects")
                next_url = urllib.parse.urljoin(target, location)
                try:
                    next_url = base.normalize_url(next_url)
                except base.ResolverError:
                    raise base.UpstreamError("Google Drive redirect is not a valid URL")
                if not self.allowlist.allows(_host(next_url)):
                    raise base.UpstreamError(
                        "Google Drive redirect target is outside its download hosts")
                # Loop re-runs assert_public_url before the next request.
                target = next_url
                continue
            if isinstance(body, str):
                body = body.encode("utf-8", "replace")
            return status, headers, target, body[:MAX_PROBE_BYTES]
        raise base.UpstreamError("Google Drive returned too many redirects")
