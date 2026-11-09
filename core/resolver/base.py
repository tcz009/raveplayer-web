# -*- coding: utf-8 -*-
"""Shared contracts and safety rails for the Rave URL resolvers.

A resolver takes a page/enclosure URL that a user or feed handed us and
returns a *direct* media URL plus the request headers a player should use.
The web service in :mod:`core.resolver.service` is the only caller.

Everything here is deliberately standard-library only.  Adapters may use the
small :class:`HTTPClient` seam for metadata fetches, but the resolver service
itself never proxies media bytes and never follows redirects for a client.

Safety rules that every adapter inherits:

* http(s) only, no credentials in the URL, :data:`MAX_URL_LENGTH` bytes max;
* hosts are allowlisted by the adapter (exact host or a parent domain);
* loopback/private/link-local/reserved addresses are always rejected, even
  when ``RAVEWEB_ALLOW_PRIVATE=1`` is set for the page fetcher -- this service
  returns URLs to players on other machines;
* response headers are sanitised so an adapter cannot smuggle a Cookie,
  Authorization, Host or hop-by-hop header back to a client;
* request timeouts are clamped to a small bounded range.
"""

import collections.abc
import http.client
import ipaddress
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

# It is tempting to accept long podcast query strings here; keep a hard cap so
# a single request cannot be used to build unbounded cache keys/log lines.
MAX_URL_LENGTH = 2048
DEFAULT_TIMEOUT = 5.0
MIN_TIMEOUT = 0.5
MAX_TIMEOUT = 15.0
AUDIO_SUFFIXES = (".mp3", ".m4a")
USER_AGENT = "RavePlayer/3.0 (resolver; +https://github.com/tcz009/plugin.audio.raveplayer)"

MAX_HEADERS = 24
MAX_HEADER_VALUE = 1024
MAX_HEADER_BYTES = 8 * 1024

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")

# Response headers that must never be replayed to a browser/Kodi client.  A
# resolver's job is "URL + how to request it", not "here is a credential".
_FORBIDDEN_HEADERS = frozenset((
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "host", "connection", "content-length", "content-type",
    "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection",
    "keep-alive", "x-forwarded-for", "x-forwarded-host", "x-real-ip",
    "forwarded",
))

# Kept local as a fallback for the (rare) case where core.fetch cannot be
# imported.  The web fetcher's own list is reused when available so the two
# services cannot drift on what "private" means.
_FALLBACK_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in (
        "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24",
        "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
        "2001:db8::/32",
    )
)

try:  # Reuse the stream proxy's address policy when this is the web build.
    from ..fetch import _address_blocked as _web_address_blocked
except Exception:  # pragma: no cover - only when core.fetch is unavailable
    _web_address_blocked = None


# --------------------------------------------------------------------------- errors


class ResolverError(Exception):
    """Base class for errors that map to a public API response.

    ``code`` and ``status`` are part of the HTTP contract.  ``message`` is
    safe to return to a client: adapters must not put signed query strings in
    it.
    """

    code = "error"
    status = 502

    def __init__(self, message="", *, code=None, status=None, retry_after=None):
        self.code = code or type(self).code
        self.status = int(status or type(self).status)
        self.retry_after = retry_after
        text = str(message or "").strip() or self.code.replace("_", " ")
        self.message = text
        super().__init__(text)

    def payload(self):
        data = {"ok": False, "error": self.message, "code": self.code}
        if self.retry_after is not None:
            data["retry_after"] = max(1, int(self.retry_after))
        return data


class InvalidRequest(ResolverError):
    """Missing, oversized or syntactically unusable request URL."""

    code = "invalid"
    status = 400


class UnknownURL(ResolverError):
    """No adapter recognized the URL (including arbitrary hosts)."""

    code = "unknown"
    status = 400


class BlockedURL(ResolverError):
    """Scheme, credentials, host allowlist or SSRF policy rejected the URL."""

    code = "blocked"
    status = 400


class UnsupportedMedia(ResolverError):
    """Known provider, but the URL is a page/playlist, not playable audio."""

    code = "unsupported"
    status = 400


class Unauthorized(ResolverError):
    """The request did not carry a valid web session."""

    code = "unauthorized"
    status = 401


class NotFound(ResolverError):
    """The provider says the media no longer exists (404/410 upstream)."""

    code = "not_found"
    status = 404


class RateLimited(ResolverError):
    """The per-host token bucket is empty."""

    code = "rate_limited"
    status = 429


class UpstreamError(ResolverError):
    """A provider failed, returned an unexpected payload, or was unreachable."""

    code = "upstream"
    status = 502


class UpstreamTimeout(UpstreamError):
    """A provider did not answer inside the bounded timeout."""

    code = "timeout"
    status = 504


# --------------------------------------------------------------------------- values


class Resolved(object):
    """The result of a successful resolution.

    ``expires_at`` is a Unix timestamp.  It may be sooner than the cache TTL
    when a provider hands out a signed/expiring URL.
    """

    __slots__ = ("url", "headers", "expires_at", "source")

    def __init__(self, url, headers=None, expires_at=0.0, source=""):
        self.url = url
        self.headers = dict(headers or {})
        self.expires_at = expires_at
        self.source = source

    def __eq__(self, other):
        if not isinstance(other, Resolved):
            return NotImplemented
        return (self.url, self.headers, self.expires_at, self.source) == (
            other.url, other.headers, other.expires_at, other.source)

    def __repr__(self):
        # Never show the query string: it can contain signed tokens.
        return "Resolved(url=%r, headers=%r, expires_at=%r, source=%r)" % (
            _safe_url(self.url), self.headers, self.expires_at, self.source)


try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8 is not supported
    Protocol = object

    def runtime_checkable(cls):
        return cls


@runtime_checkable
class Resolver(Protocol):
    """The one interface every adapter implements.

    ``matches`` must be cheap and side-effect free: it is called for every
    request before the cache and rate limiter.  It must never perform DNS or
    network I/O.  ``resolve`` performs the (bounded) work and returns a
    :class:`Resolved`.
    """

    source = ""

    def matches(self, url):
        """Return True when this adapter owns *url*."""
        raise NotImplementedError

    def resolve(self, url):
        """Return a :class:`Resolved` or raise a :class:`ResolverError`."""
        raise NotImplementedError


class HTTPResponse(object):
    """Minimal response value returned by an :class:`HTTPClient`."""

    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status, headers=None, body=b"", url=""):
        self.status = int(status)
        self.headers = dict(headers or {})
        self.body = body or b""
        self.url = url

    def header(self, name, default=""):
        wanted = str(name).lower()
        for key, value in self.headers.items():
            if str(key).lower() == wanted:
                return value
        return default


class HTTPClient(Protocol):
    """Tiny seam so adapter tests never touch the network."""

    def request(self, method, url, headers=None, timeout=None):
        """Return an :class:`HTTPResponse`; raise a ResolverError on failure."""
        raise NotImplementedError


# --------------------------------------------------------------------------- URL rules


def clamp_timeout(value, default=DEFAULT_TIMEOUT):
    """Clamp an operator-supplied timeout to the service's bounded range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, number))


def env_hosts(name, default=()):
    """Read a comma-separated host allowlist from *name*.

    An unset variable returns *default*; a set-but-empty variable returns an
    empty tuple, which disables that adapter's direct-audio path.  This makes
    ``RESOLVER_DIRECT_HOSTS=`` a deliberate "turn it off" switch.
    """
    raw = os.environ.get(name)
    if raw is None:
        return tuple(default)
    result = []
    for item in raw.split(","):
        host = item.strip().lower().rstrip(".")
        if host and host not in result:
            result.append(host)
    return tuple(result)


def split_url(raw):
    """Parse and apply the cheap syntactic rules shared by all adapters."""
    if not isinstance(raw, str):
        raise InvalidRequest("url must be a string")
    text = raw.strip()
    if not text:
        raise InvalidRequest("url is required")
    if len(text) > MAX_URL_LENGTH:
        raise InvalidRequest("url is too long")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        raise InvalidRequest("url contains whitespace or control characters")
    # urllib treats a backslash as a normal path char while browsers/proxies
    # sometimes treat it as a slash.  Ambiguous URLs are not worth the risk.
    if "\\" in text:
        raise InvalidRequest("url contains a backslash")
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        raise InvalidRequest("url is malformed")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnknownURL("only http(s) URLs are supported")
    try:
        host = parts.hostname
    except ValueError:
        raise InvalidRequest("url has an invalid host")
    if not host:
        raise InvalidRequest("url has no host")
    if parts.username is not None or parts.password is not None:
        raise BlockedURL("credentials in URLs are not allowed")
    if len(host) > 253:
        raise InvalidRequest("url host is too long")
    try:
        port = parts.port
    except ValueError:
        raise InvalidRequest("url has an invalid port")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidRequest("url has an invalid port")
    return parts


def normalize_url(raw):
    """Return a canonical http(s) URL with credentials/fragments/default port
    removed and the host lower-cased (IDNA for non-ASCII names)."""
    parts = split_url(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidRequest("url has no host")
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        raise InvalidRequest("url host is not valid IDNA")
    try:
        port = parts.port
    except ValueError:
        raise InvalidRequest("url has an invalid port")
    scheme = parts.scheme.lower()
    if ":" in host:
        netloc_host = "[%s]" % host
    else:
        netloc_host = host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = "%s:%d" % (netloc_host, port)
    else:
        netloc = netloc_host
    path = parts.path or "/"
    normalized = urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))
    if len(normalized) > MAX_URL_LENGTH:
        raise InvalidRequest("url is too long")
    return normalized


def url_host(url):
    """Best-effort lower-case host for response metadata; never raises."""
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except (ValueError, AttributeError):
        return ""


def _safe_url(url):
    """Host/path only, for debug reprs -- query strings may hold tokens."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "<unparsable>"
    host = parts.hostname or ""
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def is_audio_url(url, suffixes=AUDIO_SUFFIXES):
    """True when the URL path ends in a supported audio extension."""
    try:
        path = urllib.parse.urlsplit(str(url)).path.lower()
    except ValueError:
        return False
    return any(path.endswith(suffix) for suffix in suffixes)


class HostAllowlist(object):
    """Exact-host allowlist with dot-boundary subdomain matching.

    ``example.com`` allows ``example.com`` and ``cdn.example.com`` but not
    ``notexample.com``.  Wildcards are not supported on purpose: an operator
    cannot accidentally turn the service into an open relay.
    """

    def __init__(self, hosts=()):
        if isinstance(hosts, str):
            hosts = (hosts,)
        cleaned = []
        for value in hosts or ():
            host = str(value or "").strip().lower().rstrip(".")
            if host.startswith("*."):
                raise ValueError("wildcards are not supported in resolver allowlists")
            if host.startswith("."):
                host = host.lstrip(".")
            if not host:
                continue
            if any(ch in host for ch in "/\\@* \t\r\n"):
                raise ValueError("invalid host in resolver allowlist: %r" % (value,))
            if ":" not in host and not _valid_dns_name(host):
                raise ValueError("invalid host in resolver allowlist: %r" % (value,))
            if host not in cleaned:
                cleaned.append(host)
        self.hosts = tuple(cleaned)

    def allows(self, host):
        host = str(host or "").strip().lower().rstrip(".")
        if not host:
            return False
        return any(host == allowed or host.endswith("." + allowed)
                   for allowed in self.hosts)

    def __bool__(self):
        return bool(self.hosts)

    __nonzero__ = __bool__

    def __len__(self):
        return len(self.hosts)

    def __iter__(self):
        return iter(self.hosts)

    def __repr__(self):
        return "HostAllowlist(%r)" % (self.hosts,)


def _valid_dns_name(host):
    labels = host.split(".")
    if not labels or len(labels) > 127:
        return False
    if host.endswith("."):
        labels = labels[:-1]
    for label in labels:
        if not label or len(label) > 63:
            return False
        if not _HOST_LABEL_RE.match(label):
            return False
    return True


# --------------------------------------------------------------------------- SSRF rules


def address_blocked(address):
    """True for addresses the resolver must never hand back.

    The web fetcher's implementation is reused when importable, keeping the
    stream proxy and resolver policies in sync (including IPv4-mapped IPv6).
    """
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        try:
            address = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError:
            return True
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if _web_address_blocked is not None:
        try:
            return bool(_web_address_blocked(address))
        except Exception:  # pragma: no cover - defensive
            pass
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved
            or address.is_unspecified):
        return True
    return any(address in network for network in _FALLBACK_BLOCKED_NETWORKS)


def is_private_hostname(host):
    """Reject loopback names, single-label names and literal private IPs.

    This is the no-DNS half of the guard, so it is cheap enough for the
    service to run before adapter matching.
    """
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    if host.endswith((".localhost", ".local", ".localdomain", ".internal",
                      ".home.arpa")):
        return True
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return address_blocked(literal)
    # Docker/Kubernetes service names and other intranet hostnames are
    # single-label; public resolver URLs always have a dot.
    if "." not in host:
        return True
    return False


def _lookup_addresses(host, port, dns):
    resolver = dns or socket.getaddrinfo
    try:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except TypeError:
            infos = resolver(host, port)
    except socket.timeout:
        raise UpstreamTimeout("DNS lookup timed out")
    except (socket.gaierror, UnicodeError, OSError):
        raise BlockedURL("host could not be resolved")
    addresses = []
    for info in infos or ():
        if isinstance(info, str):
            raw = info
        else:
            try:
                raw = info[4][0]
            except (IndexError, TypeError):
                raise BlockedURL("host resolved to an unusable address")
        raw = str(raw).split("%", 1)[0]
        addresses.append(raw)
    return addresses


def check_public_host(host, port=443, dns=None):
    """Resolve *host* and fail closed unless every address is public."""
    host = str(host or "").strip().lower().rstrip(".")
    if is_private_hostname(host):
        raise BlockedURL("private or loopback hosts are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if address_blocked(literal):
            raise BlockedURL("private or loopback hosts are not allowed")
        return host
    addresses = _lookup_addresses(host, port, dns)
    if not addresses:
        raise BlockedURL("host did not resolve to a public address")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise BlockedURL("host resolved to an unusable address")
        if address_blocked(address):
            raise BlockedURL("host resolves to a private or reserved address")
    return host


def assert_public_url(url, dns=None):
    """Normalise *url* and reject private/loopback destinations.

    DNS checking is injectable for offline tests.  This intentionally has no
    ``allow_private`` switch: unlike the page fetcher, the resolver returns
    URLs to clients on other hosts.
    """
    normalized = normalize_url(url)
    parts = urllib.parse.urlsplit(normalized)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    check_public_host(host, port, dns)
    return normalized


# --------------------------------------------------------------------------- headers


def sanitize_headers(headers):
    """Copy adapter headers, rejecting anything unsafe to replay.

    Raises ``ValueError`` for invalid input; the service maps that to a 502
    because it means an adapter produced something it should not have.
    """
    if headers is None:
        return {}
    if not isinstance(headers, collections.abc.Mapping):
        raise ValueError("headers must be a mapping")
    if len(headers) > MAX_HEADERS:
        raise ValueError("too many response headers")
    out = {}
    total = 0
    for key, value in headers.items():
        name = str(key or "").strip()
        text = str(value or "").strip()
        if not name or len(name) > 64 or not _HEADER_NAME_RE.match(name):
            raise ValueError("invalid response header name")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError("forbidden response header")
        if (any(ch in text for ch in "\r\n\x00") or len(text) > MAX_HEADER_VALUE):
            raise ValueError("invalid response header value")
        total += len(name) + len(text)
        if total > MAX_HEADER_BYTES:
            raise ValueError("response headers are too large")
        out[name] = text
    return out


def validate_resolved(resolved, now=None):
    """Validate/repair an adapter result, or raise :class:`UpstreamError`."""
    if not isinstance(resolved, Resolved):
        raise UpstreamError("resolver returned an invalid result")
    try:
        normalized = normalize_url(resolved.url)
    except ResolverError:
        raise UpstreamError("resolver returned an invalid URL")
    source = str(resolved.source or "").strip()
    if not source:
        raise UpstreamError("resolver did not name its source")
    try:
        headers = sanitize_headers(resolved.headers)
    except ValueError:
        raise UpstreamError("resolver returned unsafe headers")
    try:
        expires = float(resolved.expires_at)
    except (TypeError, ValueError):
        raise UpstreamError("resolver returned an invalid expiry")
    if not math.isfinite(expires) or expires <= 0:
        raise UpstreamError("resolver returned an invalid expiry")
    if now is not None and expires <= now:
        raise UpstreamError("resolver returned an expired URL")
    return Resolved(normalized, headers, expires, source)


# --------------------------------------------------------------------------- HTTP seam


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: adapters inspect 3xx explicitly if they care."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, "redirects are not followed", headers, fp)


class UrllibHTTPClient(object):
    """Bounded, redirect-free HTTP client for adapter metadata requests.

    The resolver service does not stream media, so responses are capped at a
    small size.  Every URL is re-checked against the SSRF guard immediately
    before the request.  ``dns`` is injectable for offline tests.
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT, max_bytes=1024 * 1024, dns=None):
        self.timeout = clamp_timeout(timeout)
        self.max_bytes = max(1024, int(max_bytes))
        self.dns = dns
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, url, headers=None, timeout=None):
        method = str(method or "GET").upper()
        if method not in ("GET", "HEAD"):
            raise ValueError("only GET and HEAD are supported")
        budget = clamp_timeout(timeout if timeout is not None else self.timeout)
        target = assert_public_url(url, dns=self.dns)
        request = urllib.request.Request(
            target, headers=sanitize_headers(headers), method=method)
        try:
            response = self._opener.open(request, timeout=budget)
        except urllib.error.HTTPError as exc:
            response = exc
        except socket.timeout:
            raise UpstreamTimeout("upstream request timed out")
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), socket.timeout):
                raise UpstreamTimeout("upstream request timed out")
            raise UpstreamError("upstream request failed")
        except (http.client.HTTPException, OSError, ValueError):
            raise UpstreamError("upstream request failed")
        if method == "HEAD":
            body = b""
        else:
            # Enforce a wall-clock deadline, not just a per-recv socket
            # timeout: a provider that drips bytes must not pin a worker.
            body = self._read_body(response, time.monotonic() + budget)
        return HTTPResponse(
            status=getattr(response, "status", None) or getattr(response, "code", 0),
            headers=dict(getattr(response, "headers", {}) or {}),
            body=body,
            url=getattr(response, "url", target),
        )

    def _read_body(self, response, deadline):
        chunks = []
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise UpstreamTimeout("upstream response timed out")
            remaining = self.max_bytes + 1 - total
            if remaining <= 0:
                raise UpstreamError("upstream response was too large")
            try:
                chunk = response.read1(min(64 * 1024, remaining))
            except AttributeError:  # pragma: no cover - minimal fakes
                chunk = response.read(min(64 * 1024, remaining))
            except socket.timeout:
                raise UpstreamTimeout("upstream response timed out")
            except (http.client.HTTPException, OSError):
                raise UpstreamError("upstream response failed")
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
        if len(body) > self.max_bytes:
            raise UpstreamError("upstream response was too large")
        return body
