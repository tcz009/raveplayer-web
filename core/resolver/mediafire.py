# -*- coding: utf-8 -*-
"""Mediafire adapter for the resolver service.

Wraps the curated Mediafire page resolver (:mod:`core.ezgarage`) so the
resolver exposes one service contract for web and Kodi.
"""

from . import base

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TTL = 2 * 60 * 60
HOSTS = ("mediafire.com",)


def _host(url):
    try:
        return (base.url_host(url) or "").lower().rstrip(".")
    except Exception:
        return ""


class MediafireResolver(object):
    """Resolve a Mediafire file page to a signed direct URL."""

    source = "mediafire"
    headers = {"User-Agent": UA, "Referer": "https://www.mediafire.com/"}

    def __init__(self, timeout=None, http=None, dns=None, clock=None):
        self.timeout = timeout
        self.clock = clock

    def matches(self, url):
        host = _host(url)
        return host == "mediafire.com" or host.endswith(".mediafire.com")

    def resolve(self, url):
        from core import ezgarage
        page = str(url or "").strip()
        if not self.matches(page):
            raise base.UnknownURL("not a Mediafire file page")
        try:
            signed = ezgarage.resolve_page(page)
        except ezgarage.EzGarageError as exc:
            code = int(getattr(exc, "code", 502) or 502)
            if code == 404:
                raise base.NotFound(exc.message)
            if code == 503:
                raise base.RateLimited(exc.message,
                                       retry_after=getattr(exc, "retry_after", None))
            if code == 429:
                raise base.RateLimited(exc.message,
                                       retry_after=getattr(exc, "retry_after", None))
            raise base.UpstreamError(exc.message)
        if not signed:
            raise base.NotFound("Mediafire did not return a download link")
        now = (self.clock() if callable(self.clock) else __import__("time").time())
        return base.Resolved(signed, dict(self.headers), now + TTL, "mediafire")
