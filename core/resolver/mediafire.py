# -*- coding: utf-8 -*-
"""Mediafire adapter for the resolver service.

The adapter fetches a Mediafire file page through the **injected, bounded**
HTTP client (:class:`core.resolver.base.UrllibHTTPClient`) and extracts the
short-lived signed ``download*.mediafire.com`` URL.  It deliberately does not
use :func:`core.ezgarage._fetch_page`: that legacy helper uses ``urlopen`` with
a per-recv timeout, follows redirects and serialises every worker behind one
process-global lock (see ``docs/RESOLVER-SECURITY-1.6.62.md``).

Safety properties:

* only ``*.mediafire.com`` file/download pages are matched (no I/O in
  ``matches``);
* every page and result URL is checked against the SSRF policy and the
  provider allowlist;
* redirects are **refused**, never followed;
* the body cap and the wall-clock deadline come from the bounded client (the
  default service gives it a 4 MiB cap and a <= 15 s budget);
* no process-global lock/politeness sleep: provider pacing is the service's
  per-registrable-domain token bucket, not a global fetch lock.

Page parsing is still shared with :mod:`core.ezgarage`
(``signed_url_from_page``), so a Mediafire HTML change only needs one fix.
"""

import email.utils
import socket
import time

from . import base

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TTL = 2 * 60 * 60
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_BACKOFF = 120
HOSTS = ("mediafire.com",)
PAGE_PREFIXES = ("/file/", "/download/")

# Signed download URLs live on ``downloadN.mediafire.com``; the allowlist is
# the provider apex so a future ``downloadN`` name does not need a code change.
ALLOWLIST = base.HostAllowlist(HOSTS)
PAGE_HEADERS = base.sanitize_headers({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Referer": "https://dnbforum.com/threads/ez-old-skool-garage-sets.43637/",
})


def _host(url):
    try:
        return (base.url_host(url) or "").lower().rstrip(".")
    except Exception:
        return ""


def _retry_after(headers, default=30):
    value = ""
    for name, header in (headers or {}).items():
        if str(name).lower() == "retry-after":
            value = str(header or "").strip()
            break
    if not value:
        return default
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            when = email.utils.parsedate_to_datetime(value)
            seconds = int(when.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            seconds = default
    return max(1, min(seconds, MAX_BACKOFF))


class MediafireResolver(object):
    """Resolve a Mediafire file page to a signed direct URL."""

    source = "mediafire"
    allowlist = ALLOWLIST
    headers = {"User-Agent": UA, "Referer": "https://www.mediafire.com/"}

    def __init__(self, timeout=None, http=None, dns=None, clock=None):
        self.timeout = base.clamp_timeout(timeout)
        self.http = http
        self.dns = dns
        self.clock = clock if callable(clock) else time.time

    def matches(self, url):
        """Cheap: Mediafire host plus a file/download page path; no I/O."""
        try:
            parts = base.split_url(url)
        except base.ResolverError:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        if not (host == "mediafire.com" or host.endswith(".mediafire.com")):
            return False
        return (parts.path or "").startswith(PAGE_PREFIXES)

    def resolve(self, url):
        page = base.normalize_url(url)
        if not self.matches(page):
            raise base.UnsupportedMedia("not a Mediafire file page")
        # Reuse the curated module's HTML parser only; the fetch is ours.
        from core import ezgarage
        body = self._fetch_page(page)
        signed = ezgarage.signed_url_from_page(body.decode("utf-8", "replace"))
        if not signed:
            raise base.UpstreamError(
                "Mediafire's page no longer contains a direct download link")
        try:
            parts = base.split_url(signed)
        except base.ResolverError:
            raise base.UpstreamError("Mediafire returned an invalid download URL")
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme != "https" or not self.allowlist.allows(host)
                or not (host == "mediafire.com" or host.endswith(".mediafire.com"))
                or not base.is_audio_url(signed)):
            raise base.UpstreamError(
                "Mediafire returned a URL outside its download host")
        expires = self.clock() + TTL
        return base.Resolved(signed, dict(self.headers), expires, self.source)

    # -- bounded page fetch ------------------------------------------------

    def _fetch_page(self, page):
        """GET one page through the injected client and return its bytes.

        The client owns the SSRF check, the no-redirect policy, the timeout
        and the body cap.  This method maps HTTP statuses onto the service's
        typed error contract and refuses any 3xx explicitly (defence in depth
        if a test double/older client is used).
        """
        if self.http is None:
            raise base.UpstreamError("Mediafire resolver has no HTTP client")
        # Re-check through our own DNS seam before handing the URL to a client
        # that may be a test double.
        base.assert_public_url(page, dns=self.dns)
        try:
            response = self.http.request(
                "GET", page, headers=dict(PAGE_HEADERS), timeout=self.timeout)
        except base.ResolverError:
            raise
        except (socket.timeout, TimeoutError):
            raise base.UpstreamTimeout("Mediafire page fetch timed out")
        except OSError:
            raise base.UpstreamError("Mediafire could not be reached")
        except Exception:
            raise base.UpstreamError("Mediafire page fetch failed")

        status = int(getattr(response, "status", 0) or 0)
        headers = dict(getattr(response, "headers", {}) or {})
        body = getattr(response, "body", b"") or b""
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")

        if 300 <= status < 400:
            raise base.UpstreamError("Mediafire redirects are not followed")
        if status == 429:
            retry = _retry_after(headers)
            raise base.RateLimited(
                "Mediafire is rate-limiting requests; try again shortly",
                retry_after=retry)
        if status in (404, 410):
            raise base.NotFound("that Mediafire file is no longer available")
        if status < 200 or status >= 300:
            raise base.UpstreamError("Mediafire returned HTTP %d" % status)
        if len(body) > MAX_PAGE_BYTES:
            raise base.UpstreamError("Mediafire returned an oversized page")
        return body
