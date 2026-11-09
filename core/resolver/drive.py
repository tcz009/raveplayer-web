# -*- coding: utf-8 -*-
"""Google Drive adapter for the resolver service."""

from . import base

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TTL = 2 * 60 * 60
HOSTS = ("drive.google.com", "docs.google.com")


def _host(url):
    try:
        return (base.url_host(url) or "").lower().rstrip(".")
    except Exception:
        return ""


class DriveResolver(object):
    """Resolve a Drive file id/page to a short-lived direct audio URL."""

    source = "drive"
    headers = {"User-Agent": UA}

    def __init__(self, timeout=None, http=None, dns=None, clock=None):
        self.timeout = timeout
        self.clock = clock

    def matches(self, url):
        host = _host(url)
        text = str(url or "")
        return (host in HOSTS or host.endswith(".google.com")) and (
            "/file/d/" in text or "id=" in text)

    def resolve(self, url):
        from core import drive as drive_mod
        if not self.matches(url):
            raise base.UnknownURL("not a Google Drive file")
        try:
            direct = drive_mod.resolve(url)
        except drive_mod.DriveError as exc:
            code = int(getattr(exc, "code", 502) or 502)
            if code == 404:
                raise base.NotFound(exc.message)
            raise base.UpstreamError(exc.message)
        if not direct:
            raise base.NotFound("Google Drive did not return a download link")
        now = (self.clock() if callable(self.clock) else __import__("time").time())
        return base.Resolved(direct, dict(self.headers), now + TTL, "drive")
