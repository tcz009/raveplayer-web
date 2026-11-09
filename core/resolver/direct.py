# -*- coding: utf-8 -*-
"""Direct http(s) audio pass-through.

This is the deliberately boring adapter: if a URL already points at an
``.mp3``/``.m4a`` file on an allowlisted public host, hand it back unchanged
(plus optional playback headers).  It never proxies bytes and never follows
redirects.

The host allowlist is the important part.  Without it, ``/api/resolve`` would
be an open redirector for any URL.  ``RESOLVER_DIRECT_HOSTS`` replaces the
curated default list; setting it to an empty string disables this adapter.
"""

import socket
import time

from . import base

DEFAULT_DIRECT_HOSTS = (
    # Rave sources the web app already links to directly.
    "20bensons.com",
    "demodulated.com",
    "hardscore.com",
    "ravetapepacks.com",
    "dnbradio.com",
    "bassdrive.com",
    # Podcast/CDN hosts that serve stable audio enclosures.
    "archive.org",
    "mixcloud.com",
    "soundcloud.com",
    "megaphone.fm",
    "libsyn.com",
    "buzzsprout.com",
    "podbean.com",
    "acast.com",
    "simplecast.com",
    "transistor.fm",
    "fireside.fm",
    "omny.fm",
    "art19.com",
    "anchor.fm",
    "spotifycdn.com",
    # Direct-file hosts.  Pages on these hosts are *not* matched because the
    # path must end in an audio extension; provider adapters handle pages.
    "mediafire.com",
    "mega.nz",
    "mega.co.nz",
    "drive.usercontent.google.com",
)


class DirectResolver(object):
    """Pass through allowlisted http(s) audio URLs."""

    source = "direct"

    def __init__(self, allowed_hosts=None, headers=None, ttl=3600.0,
                 timeout=base.DEFAULT_TIMEOUT, http=None, verify=False,
                 dns=None, clock=time.time):
        if allowed_hosts is None:
            allowed_hosts = base.env_hosts(
                "RESOLVER_DIRECT_HOSTS", DEFAULT_DIRECT_HOSTS)
        self.allowlist = base.HostAllowlist(allowed_hosts)
        # Config-supplied headers (e.g. a Referer some CDN requires).  They
        # are sanitised up front so a typo cannot become a response header.
        self.headers = base.sanitize_headers(headers)
        self.ttl = max(1.0, float(ttl))
        self.timeout = base.clamp_timeout(timeout)
        self.http = http
        self.verify = bool(verify)
        self.dns = dns
        self.clock = clock

    def matches(self, url):
        """Cheap check: audio path + allowlisted host; no DNS."""
        try:
            parts = base.split_url(url)
        except base.ResolverError:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or not self.allowlist.allows(host):
            return False
        return base.is_audio_url(url)

    def resolve(self, url):
        normalized = base.normalize_url(url)
        host = base.url_host(normalized)
        if not self.allowlist.allows(host):
            raise base.BlockedURL("host is not on the direct-audio allowlist")
        if not base.is_audio_url(normalized):
            raise base.UnsupportedMedia("not an mp3/m4a audio URL")
        base.assert_public_url(normalized, dns=self.dns)
        if self.verify:
            self._probe(normalized)
        expires = self.clock() + self.ttl
        return base.Resolved(
            url=normalized,
            headers=dict(self.headers),
            expires_at=expires,
            source=self.source,
        )

    def _probe(self, url):
        """Optional HEAD check; disabled by default (pass-through is cheap)."""
        if self.http is None:
            raise base.UpstreamError("audio verification is enabled without an HTTP client")
        try:
            response = self.http.request(
                "HEAD", url, headers=dict(self.headers), timeout=self.timeout)
        except (socket.timeout, TimeoutError):
            raise base.UpstreamTimeout("audio probe timed out")
        except base.ResolverError:
            raise
        except Exception:
            raise base.UpstreamError("audio probe failed")
        status = getattr(response, "status", 0)
        if 300 <= status < 400:
            raise base.UpstreamError("upstream redirect is not followed")
        if status in (404, 410):
            raise base.NotFound("audio file was not found upstream")
        if status in (401, 403):
            raise base.UpstreamError("upstream refused the audio probe")
        if status >= 500:
            raise base.UpstreamError("upstream audio probe failed")
        if status >= 400:
            raise base.NotFound("audio file was not found upstream")
        content_type = ""
        try:
            content_type = (response.header("Content-Type") or "").lower()
        except AttributeError:
            content_type = ""
        if content_type and not (
                content_type.startswith("audio/")
                or content_type.startswith("application/octet-stream")
                or content_type.startswith("binary/octet-stream")):
            raise base.UnsupportedMedia("upstream did not return audio")
