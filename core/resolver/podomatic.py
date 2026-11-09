# -*- coding: utf-8 -*-
"""Podomatic / pdcn.co enclosure resolver.

Podomatic feeds expose two shapes:

* ``https://<show>.podomatic.com/enclosure/<date>.mp3?_=...``
* ``https://pdcn.co/e/<show>.podomatic.com/enclosure/<date>.mp3?_=...``

The ``pdcn.co/e/`` wrapper is a 301 to the canonical Podomatic host.  We
canonicalise it locally (no redirect on every playback), validate that the
result is an enclosure under ``*.podomatic.com``, and refuse episode pages,
show pages, feeds and playlists.  Other agents implement the mediafire,
drive and mega adapters; this module owns only Podomatic.
"""

import socket
import time
import urllib.parse

from . import base

PODOMATIC_HOSTS = ("podomatic.com", "pdcn.co")
_ENCLOSURE_PREFIX = "/enclosure/"
_PAGE_SUFFIXES = (
    ".m3u", ".m3u8", ".pls", ".rss", ".xml", ".html", ".htm", ".json",
)
_DEFAULT_HEADERS = {
    "User-Agent": base.USER_AGENT,
    "Accept": "audio/mpeg, audio/mp4;q=0.9, audio/*;q=0.8, */*;q=0.1",
    # Podomatic's edge has occasionally required a same-site referer for
    # enclosure downloads; it is harmless for direct S3-backed enclosures.
    "Referer": "https://www.podomatic.com/",
}


class PodomaticResolver(object):
    """Resolve Podomatic enclosure URLs to a canonical direct media URL."""

    source = "podomatic"

    def __init__(self, headers=None, ttl=3600.0, timeout=base.DEFAULT_TIMEOUT,
                 http=None, verify=False, dns=None, clock=time.time):
        merged = dict(_DEFAULT_HEADERS)
        merged.update(headers or {})
        self.headers = base.sanitize_headers(merged)
        # Canonical enclosures must stay on a Podomatic host.  The service
        # enforces this on the resolved result as defence in depth.
        self.allowlist = base.HostAllowlist(("podomatic.com",))
        self.ttl = max(1.0, float(ttl))
        self.timeout = base.clamp_timeout(timeout)
        self.http = http
        self.verify = bool(verify)
        self.dns = dns
        self.clock = clock

    def matches(self, url):
        try:
            parts = base.split_url(url)
        except base.ResolverError:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        return (host == "podomatic.com" or host.endswith(".podomatic.com")
                or host == "pdcn.co")

    def resolve(self, url):
        canonical = self._canonical(url)
        if not base.is_audio_url(canonical):
            raise base.UnsupportedMedia(
                "Podomatic episode pages and playlists are not audio files")
        base.assert_public_url(canonical, dns=self.dns)
        if self.verify:
            self._probe(canonical)
        return base.Resolved(
            url=canonical,
            headers=dict(self.headers),
            expires_at=self.clock() + self.ttl,
            source=self.source,
        )

    def _canonical(self, url):
        parts = base.split_url(url)
        host = (parts.hostname or "").lower().rstrip(".")
        path = parts.path or "/"
        host_is_podomatic = (host == "podomatic.com"
                             or host.endswith(".podomatic.com"))

        if host == "pdcn.co":
            return self._canonical_pdcn(parts, path)
        if not host_is_podomatic:
            raise base.BlockedURL("not a Podomatic host")

        # Episode/show pages, feeds and playlists are common user input and
        # must never be treated as media.
        lowered = path.lower().rstrip("/")
        if "/episodes/" in lowered or lowered.endswith("/episodes"):
            raise base.UnsupportedMedia("Podomatic episode pages are not playable media")
        if lowered.endswith(_PAGE_SUFFIXES) or lowered.endswith("/feed"):
            raise base.UnsupportedMedia("Podomatic pages and playlists are not media")
        if not path.lower().startswith(_ENCLOSURE_PREFIX):
            raise base.UnsupportedMedia("not a Podomatic enclosure URL")
        if not base.is_audio_url(path):
            raise base.UnsupportedMedia("Podomatic enclosure is not mp3/m4a")

        # Force https: Podomatic has served plain-http enclosures, but the
        # canonical host supports TLS and Kodi/AVPlayer prefer it.
        return urllib.parse.urlunsplit(
            ("https", host, path, parts.query, ""))

    def _canonical_pdcn(self, parts, path):
        if not path.startswith("/e/"):
            raise base.UnsupportedMedia("pdcn.co links must use the /e/ form")
        rest = path[3:]
        # pdcn paths may be plain (host/path) or percent-encoded absolute
        # URLs.  Only decode when the encoded form is an absolute URL, so a
        # legitimate ``%20`` in a filename survives into the canonical URL.
        if rest.lower().startswith(("http%3a", "https%3a")) or "%2f%2f" in rest.lower():
            for _ in range(2):
                decoded = urllib.parse.unquote(rest)
                if decoded == rest:
                    break
                rest = decoded
        if not rest:
            raise base.UnsupportedMedia("pdcn.co link has no target")
        if "://" not in rest:
            rest = "https://" + rest
        try:
            inner = base.split_url(rest)
        except base.ResolverError:
            raise base.UnsupportedMedia("pdcn.co link target is malformed")
        target_host = (inner.hostname or "").lower().rstrip(".")
        if not (target_host == "podomatic.com"
                or target_host.endswith(".podomatic.com")):
            raise base.UnsupportedMedia("pdcn.co link does not target Podomatic")
        target_path = inner.path or "/"
        if not target_path.lower().startswith(_ENCLOSURE_PREFIX):
            raise base.UnsupportedMedia("pdcn.co link is not a Podomatic enclosure")
        if target_path.lower().rstrip("/").endswith(_PAGE_SUFFIXES):
            raise base.UnsupportedMedia("pdcn.co link is a playlist, not audio")
        if not base.is_audio_url(target_path):
            raise base.UnsupportedMedia("pdcn.co envelope is not mp3/m4a")
        query = inner.query or parts.query
        return urllib.parse.urlunsplit(
            ("https", target_host, target_path, query, ""))

    def _probe(self, url):
        if self.http is None:
            raise base.UpstreamError("audio verification is enabled without an HTTP client")
        try:
            response = self.http.request(
                "HEAD", url, headers=dict(self.headers), timeout=self.timeout)
        except (socket.timeout, TimeoutError):
            raise base.UpstreamTimeout("Podomatic probe timed out")
        except base.ResolverError:
            raise
        except Exception:
            raise base.UpstreamError("Podomatic probe failed")
        status = getattr(response, "status", 0)
        if 300 <= status < 400:
            raise base.UpstreamError("upstream redirect is not followed")
        if status in (404, 410):
            raise base.NotFound("Podomatic enclosure was not found upstream")
        if status in (401, 403):
            raise base.UpstreamError("Podomatic refused the audio probe")
        if status >= 500:
            raise base.UpstreamError("Podomatic audio probe failed")
        if status >= 400:
            raise base.NotFound("Podomatic enclosure was not found upstream")
        content_type = ""
        try:
            content_type = (response.header("Content-Type") or "").lower()
        except AttributeError:
            content_type = ""
        if content_type and not (
                content_type.startswith("audio/")
                or content_type.startswith("application/octet-stream")
                or content_type.startswith("binary/octet-stream")):
            raise base.UnsupportedMedia("Podomatic did not return audio")
