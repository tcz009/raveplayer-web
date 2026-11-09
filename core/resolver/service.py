# -*- coding: utf-8 -*-
"""The resolver HTTP service.

Mounted by ``app.py`` as a Flask blueprint::

    from core.resolver.service import resolver_bp
    app.register_blueprint(resolver_bp)

The app's existing ``before_request`` guard protects every ``/api/`` path, so
this blueprint needs no separate token.  A blueprint-local guard repeats the
session check so the service is also safe when mounted in a small test app and
fails closed if the app helper cannot be imported.

Contract (also in ``docs/RESOLVER-SERVICE.md``)::

    GET  /api/resolve?u=<url>
    POST /api/resolve   {"url": "<url>"}

    200 {"ok": true, "url": "...", "headers": {...},
         "expires": 1700000000, "source": "podomatic", "host": "..."}
    400 {"ok": false, "error": "...", "code": "unknown"|"blocked"|"invalid"|"unsupported"}
    401 {"ok": false, "error": "...", "code": "unauthorized"}
    404 {"ok": false, "error": "...", "code": "not_found"}
    429 {"ok": false, "error": "...", "code": "rate_limited", "retry_after": N}
    502 {"ok": false, "error": "...", "code": "upstream"}
    504 {"ok": false, "error": "...", "code": "timeout"}

The service does not proxy media bytes and never emits a redirect.  Clients
receive a URL they can play directly.
"""

import collections
import hashlib
import importlib
import logging
import math
import os
import threading
import time

from flask import Blueprint, current_app, jsonify, request, session

from . import base
from .cache import ResolverCache
from .direct import DirectResolver
from .podomatic import PodomaticResolver

LOG = logging.getLogger("raveweb.resolver")

DEFAULT_RATE_PER_MINUTE = 60.0
DEFAULT_BURST = 10
MAX_REQUEST_BODY = 8192
# A request URL is never stored in memory as a cache key.  The digest includes
# the source and the raw URL (so a MEGA fragment still separates two keys) but
# a dump of the cache cannot reveal a fragment key or a signed query string.
def cache_key(source, raw_url):
    material = ("%s\0%s" % (source or "", raw_url or "")).encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()

OPTIONAL_ADAPTERS = (
    ("mediafire", "MediafireResolver"),
    ("drive", "DriveResolver"),
    ("mega", "MegaResolver"),
)


# --------------------------------------------------------------------------- rate limit


class TokenBucket(object):
    """Per-key token bucket, used per target host.

    ``rate_per_minute`` may be zero to disable limiting (useful in tests and
    for a private deployment).  ``clock`` is injectable and defaults to a
    monotonic clock, so wall-clock changes cannot mint tokens.
    """

    def __init__(self, rate_per_minute=DEFAULT_RATE_PER_MINUTE,
                 burst=DEFAULT_BURST, clock=time.monotonic, max_keys=4096):
        try:
            rate = float(rate_per_minute)
        except (TypeError, ValueError):
            rate = DEFAULT_RATE_PER_MINUTE
        self.rate_per_second = max(0.0, rate) / 60.0
        try:
            burst = float(burst)
        except (TypeError, ValueError):
            burst = float(DEFAULT_BURST)
        self.burst = max(1.0, burst)
        self.clock = clock
        self.max_keys = max(1, int(max_keys))
        self._lock = threading.Lock()
        # OrderedDict + move_to_end gives cheap LRU eviction.  Without a cap a
        # caller can mint one bucket per random subdomain and keep it forever.
        self._state = collections.OrderedDict()

    def take(self, key):
        """Consume one token; return 0 on success or Retry-After seconds."""
        if self.rate_per_second <= 0:
            return 0
        key = str(key or "").lower()
        now = self.clock()
        with self._lock:
            tokens, last = self._state.get(key, (self.burst, now))
            elapsed = max(0.0, now - last)
            tokens = min(self.burst, tokens + elapsed * self.rate_per_second)
            self._state[key] = (tokens if tokens < 1.0 else tokens - 1.0, now)
            self._state.move_to_end(key)
            while len(self._state) > self.max_keys:
                self._state.popitem(last=False)
            if tokens >= 1.0:
                return 0
            wait = (1.0 - tokens) / self.rate_per_second
            return max(1, int(math.ceil(wait)))

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._state.clear()
            else:
                self._state.pop(str(key or "").lower(), None)

    def __len__(self):
        with self._lock:
            return len(self._state)


# --------------------------------------------------------------------------- service


class ResolverService(object):
    """Adapter registry + cache + rate limiter.  No Flask dependency."""

    def __init__(self, adapters=None, cache=None, limiter=None,
                 timeout=base.DEFAULT_TIMEOUT, dns=None, clock=time.time,
                 identity_quota=None):
        self.timeout = base.clamp_timeout(timeout)
        self.dns = dns
        self.clock = clock
        # Optional hook: ``identity_quota(identity) -> retry_after_seconds``
        # (0/None means allowed).  It is opt-in so small/private deployments
        # keep working unchanged; the blueprint passes the session identity.
        self.identity_quota = identity_quota
        self.cache = cache if cache is not None else ResolverCache(clock=clock)
        self.limiter = limiter if limiter is not None else TokenBucket(
            rate_per_minute=_env_float("RESOLVER_RATE_PER_MINUTE",
                                       DEFAULT_RATE_PER_MINUTE),
            burst=_env_float("RESOLVER_BURST", DEFAULT_BURST),
        )
        self.adapters = []
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter):
        if not callable(getattr(adapter, "matches", None)):
            raise TypeError("resolver has no matches()")
        if not callable(getattr(adapter, "resolve", None)):
            raise TypeError("resolver has no resolve()")
        self.adapters.append(adapter)
        return adapter

    def resolve(self, raw_url, identity=None):
        """Resolve one request URL or raise a :class:`base.ResolverError`.

        *identity* is an opaque bucket key for the optional per-identity
        quota; it is never logged or cached.
        """
        normalized = base.normalize_url(raw_url)
        host = base.url_host(normalized)

        # Cheap, no-DNS rejection before any adapter/cache work.  This is what
        # makes ``http://127.0.0.1`` a blocked 400 even if an adapter would
        # otherwise match it.
        if base.is_private_hostname(host):
            raise base.BlockedURL("private or loopback hosts are not allowed")

        # Mega keeps its decryption key in the fragment; adapters must see the
        # original URL, while host validation/cache still use the normalised form.
        adapter = self._adapter_for(raw_url)
        if adapter is None:
            raise base.UnknownURL("no resolver is configured for this URL")

        # Per-identity quota applies to every request, including cache hits:
        # it bounds request volume, not just provider work.
        if self.identity_quota is not None and identity:
            try:
                retry = self.identity_quota(identity)
            except Exception:
                LOG.warning("resolver: identity quota hook failed")
                retry = 60
            if retry:
                raise base.RateLimited(
                    "too many resolutions for this session; retry shortly",
                    retry_after=retry)

        key = cache_key(getattr(adapter, "source", "resolver"), raw_url)
        cached = self.cache.lookup(key)
        if cached is not None:
            if cached.error is not None:
                raise cached.error
            if cached.value is not None:
                return cached.value

        def factory():
            # Bucket by registrable domain: ``n1``/``n2.mediafire.com`` must
            # not each get a fresh provider budget.
            retry = self.limiter.take(base.registrable_domain(host))
            if retry:
                raise base.RateLimited(
                    "too many resolutions for this host; retry shortly",
                    retry_after=retry)
            resolved = adapter.resolve(raw_url)
            checked = base.validate_resolved(resolved, now=self.clock())
            # Every adapter must publish its direct-file allowlist.  Missing
            # or mismatched allowlists fail closed (this also covers adapters
            # registered before the hook existed).
            allowlist = getattr(adapter, "allowlist", None)
            if not isinstance(allowlist, base.HostAllowlist) or not allowlist:
                raise base.UpstreamError("resolver adapter has no allowlist")
            if not allowlist.allows(base.url_host(checked.url)):
                raise base.UpstreamError(
                    "resolver returned a URL outside its allowlist")
            try:
                # Defence in depth: an adapter must not be able to return a
                # private URL even if its own checks were skipped.
                base.assert_public_url(checked.url, dns=self.dns)
            except base.BlockedURL:
                raise base.UpstreamError("resolver returned a blocked URL")
            return checked

        return self.cache.run(key, host, factory)

    def _adapter_for(self, url):
        for adapter in self.adapters:
            try:
                if adapter.matches(url):
                    return adapter
            except Exception:
                LOG.warning("resolver adapter %r matches() failed",
                            getattr(adapter, "source", adapter))
        return None


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _env_bool(name, default=False):
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return bool(default)
    return value not in ("0", "false", "no", "off")


def build_identity_quota(rate_per_minute=120.0, burst=30, clock=time.monotonic):
    """Return the optional per-identity quota hook.

    The hook receives an opaque session identity and returns ``0`` when the
    request may proceed or a positive ``Retry-After``.  It is deliberately a
    *hook* (rather than a mandatory feature) so tests and single-user
    deployments can pass ``identity_quota=None``; the default service enables
    a moderate limit.  Set ``RESOLVER_IDENTITY_RATE_PER_MINUTE=0`` to disable.
    """
    bucket = TokenBucket(
        rate_per_minute=rate_per_minute, burst=burst, clock=clock,
        max_keys=4096)

    def quota(identity):
        # Never keep an unbounded identity string in limiter state.
        key = str(identity or "")[:128]
        return bucket.take(key) if key else 0

    quota.bucket = bucket
    return quota


def _build_optional_adapters(timeout, http, dns, clock):
    """Load mediafire/drive/mega modules if another agent has added them.

    Missing modules are simply skipped, so this worktree can land before the
    provider adapters do.  Enabled provider modules can be narrowed with
    ``RESOLVER_ADAPTERS=mediafire,drive``.
    """
    enabled = os.environ.get("RESOLVER_ADAPTERS")
    names = {item.strip().lower() for item in (enabled or "").split(",") if item.strip()}
    adapters = []
    for module_name, class_name in OPTIONAL_ADAPTERS:
        if names and module_name not in names:
            continue
        try:
            module = importlib.import_module("." + module_name, __package__)
        except ImportError:
            continue
        except Exception:
            LOG.exception("resolver: optional adapter %s failed to import", module_name)
            continue
        cls = getattr(module, class_name, None)
        if cls is None:
            LOG.warning("resolver: %s has no %s", module_name, class_name)
            continue
        try:
            adapters.append(cls(timeout=timeout, http=http, dns=dns, clock=clock))
        except TypeError:
            adapters.append(cls())
    return adapters


def build_default_service(timeout=None, dns=None, clock=time.time):
    """Build the production adapter stack (Podomatic + direct + optional)."""
    timeout = base.clamp_timeout(
        timeout if timeout is not None
        else _env_float("RESOLVER_TIMEOUT", base.DEFAULT_TIMEOUT))
    verify = _env_bool("RESOLVER_VERIFY", False)
    identity_rate = _env_float("RESOLVER_IDENTITY_RATE_PER_MINUTE", 120.0)
    identity_burst = _env_float("RESOLVER_IDENTITY_BURST", 30)
    # Constructing the client performs no I/O.  It is passed to every adapter
    # so page-based adapters (mediafire/drive/mega) have a bounded, SSRF-safe
    # HTTP seam even when the optional HEAD verification is off.  The body cap
    # matches Mediafire's 4 MiB page limit; adapters enforce their own smaller
    # caps for probes/metadata.
    http = base.UrllibHTTPClient(
        timeout=timeout, max_bytes=4 * 1024 * 1024, dns=dns)
    adapters = [
        PodomaticResolver(
            timeout=timeout, http=http, verify=verify, dns=dns, clock=clock),
    ]
    adapters.extend(_build_optional_adapters(timeout, http, dns, clock))
    adapters.append(DirectResolver(
        timeout=timeout, http=http, verify=verify, dns=dns, clock=clock))
    return ResolverService(
        adapters=adapters,
        cache=ResolverCache(
            positive_ttl=_env_float("RESOLVER_CACHE_TTL", 300.0),
            negative_ttl=_env_float("RESOLVER_NEGATIVE_TTL", 15.0),
            clock=clock,
        ),
        limiter=TokenBucket(
            rate_per_minute=_env_float("RESOLVER_RATE_PER_MINUTE",
                                       DEFAULT_RATE_PER_MINUTE),
            burst=_env_float("RESOLVER_BURST", DEFAULT_BURST),
        ),
        timeout=timeout,
        dns=dns,
        clock=clock,
        identity_quota=(build_identity_quota(
            rate_per_minute=identity_rate, burst=identity_burst)
            if identity_rate > 0 else None),
    )


# --------------------------------------------------------------------------- auth


def _authorized():
    """Match the web app's full session auth; fail closed when unavailable.

    The mere presence of ``auth``/``sid`` in the signed cookie is **not**
    authentication: identity, role, session age, server-side revocation and
    block state all live in ``app._session_valid``.  A standalone mount that
    cannot import the app must inject ``RESOLVER_AUTH_CHECK``; otherwise every
    request is rejected.
    """
    configured = current_app.config.get("RESOLVER_AUTH_CHECK")
    if configured is not None:
        try:
            return bool(configured() if callable(configured) else configured)
        except Exception:
            return False
    try:
        app_module = importlib.import_module("app")
    except Exception:
        return False
    checker = getattr(app_module, "_session_valid", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def _request_identity():
    """Opaque session identity for the optional quota hook; never logged."""
    try:
        return str(session.get("identity") or "")[:128]
    except Exception:
        return ""


# --------------------------------------------------------------------------- blueprint


resolver_bp = Blueprint("resolver", __name__)


@resolver_bp.before_request
def _require_auth():
    if not _authorized():
        return _error_response(base.Unauthorized("authentication required"))


@resolver_bp.after_request
def _security_headers(response):
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@resolver_bp.route("/api/resolve", methods=["GET", "POST"])
def resolve_endpoint():
    try:
        raw_url = _request_url()
        resolved = _service().resolve(raw_url, identity=_request_identity())
    except base.ResolverError as exc:
        return _error_response(exc)
    except Exception:
        current_app.logger.exception("resolver: unexpected failure")
        return _error_response(base.UpstreamError("resolver failed"))
    return _success_response(resolved, raw_url)


def init_app(app):
    """Register the blueprint once; parent ``app.py`` calls this."""
    if "resolver" not in app.blueprints:
        app.register_blueprint(resolver_bp)
    return app


def _service():
    configured = current_app.config.get("RESOLVER_SERVICE")
    if configured is not None:
        return configured
    service = current_app.extensions.get("resolver_service")
    if service is None:
        service = build_default_service()
        current_app.extensions["resolver_service"] = service
    return service


def _request_url():
    if request.method == "POST":
        length = request.content_length or 0
        if length > MAX_REQUEST_BODY:
            raise base.InvalidRequest("request body is too large")
        if (request.content_length is None
                and "chunked" in (request.headers.get("Transfer-Encoding") or "").lower()):
            raise base.InvalidRequest("chunked request bodies are not supported")
        payload = request.get_json(silent=True)
        if payload is None:
            raw_url = request.form.get("url") or request.form.get("u")
        elif isinstance(payload, dict):
            raw_url = payload.get("url") or payload.get("u")
        else:
            raise base.InvalidRequest("expected a JSON object")
    else:
        raw_url = request.args.get("u") or request.args.get("url")
    if raw_url is None or isinstance(raw_url, (dict, list, tuple)):
        raise base.InvalidRequest("url is required")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise base.InvalidRequest("url is required")
    return raw_url


def _success_response(resolved, raw_url=None):
    payload = {
        "ok": True,
        "url": resolved.url,
        "headers": dict(resolved.headers),
        "expires": int(resolved.expires_at),
        "source": resolved.source,
        "host": base.url_host(resolved.url),
    }
    # MEGA's URL serves AES-CTR ciphertext, so the direct URL is not playable
    # on its own.  Add a short-lived signed stream URL for players that cannot
    # send the web session cookie.  Import locally: the stream module depends
    # on this package and Flask, and this keeps the resolver usable standalone.
    if resolved.source == "mega" and raw_url:
        try:
            from . import mega_stream
            payload["stream"] = mega_stream.signed_path(raw_url)
        except Exception:
            pass
    response = jsonify(payload)
    response.status_code = 200
    return response


def _error_response(exc):
    response = jsonify(exc.payload())
    status = int(getattr(exc, "status", 502) or 502)
    if status < 400 or status > 599:
        status = 502
    response.status_code = status
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


__all__ = [
    "ResolverService", "TokenBucket", "build_default_service",
    "build_identity_quota", "cache_key", "resolver_bp", "init_app",
]
