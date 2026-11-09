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
                 burst=DEFAULT_BURST, clock=time.monotonic):
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
        self._lock = threading.Lock()
        self._state = {}

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
            if tokens >= 1.0:
                self._state[key] = (tokens - 1.0, now)
                return 0
            self._state[key] = (tokens, now)
            wait = (1.0 - tokens) / self.rate_per_second
            return max(1, int(math.ceil(wait)))

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._state.clear()
            else:
                self._state.pop(str(key or "").lower(), None)


# --------------------------------------------------------------------------- service


class ResolverService(object):
    """Adapter registry + cache + rate limiter.  No Flask dependency."""

    def __init__(self, adapters=None, cache=None, limiter=None,
                 timeout=base.DEFAULT_TIMEOUT, dns=None, clock=time.time):
        self.timeout = base.clamp_timeout(timeout)
        self.dns = dns
        self.clock = clock
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

    def resolve(self, raw_url):
        """Resolve one request URL or raise a :class:`base.ResolverError`."""
        normalized = base.normalize_url(raw_url)
        host = base.url_host(normalized)

        # Cheap, no-DNS rejection before any adapter/cache work.  This is what
        # makes ``http://127.0.0.1`` a blocked 400 even if an adapter would
        # otherwise match it.
        if base.is_private_hostname(host):
            raise base.BlockedURL("private or loopback hosts are not allowed")

        adapter = self._adapter_for(normalized)
        if adapter is None:
            raise base.UnknownURL("no resolver is configured for this URL")

        key = "%s:%s" % (getattr(adapter, "source", "resolver"), normalized)
        cached = self.cache.lookup(key)
        if cached is not None:
            if cached.error is not None:
                raise cached.error
            if cached.value is not None:
                return cached.value

        def factory():
            retry = self.limiter.take(host)
            if retry:
                raise base.RateLimited(
                    "too many resolutions for this host; retry shortly",
                    retry_after=retry)
            resolved = adapter.resolve(normalized)
            checked = base.validate_resolved(resolved, now=self.clock())
            # If an adapter publishes its direct-file allowlist, enforce it
            # on the result too.  This catches a provider bug that would
            # otherwise return an arbitrary (but public) host.
            allowlist = getattr(adapter, "allowlist", None)
            if allowlist is not None and hasattr(allowlist, "allows"):
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
    # Constructing the client performs no I/O.  It is passed to every adapter
    # so page-based adapters (mediafire/drive/mega) have a bounded, SSRF-safe
    # HTTP seam even when the optional HEAD verification is off.
    http = base.UrllibHTTPClient(timeout=timeout, dns=dns)
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
    )


# --------------------------------------------------------------------------- auth


def _authorized():
    """Match the web app's session auth, with a safe standalone fallback."""
    configured = current_app.config.get("RESOLVER_AUTH_CHECK")
    if configured is not None:
        try:
            return bool(configured() if callable(configured) else configured)
        except Exception:
            return False
    try:
        app_module = importlib.import_module("app")
    except Exception:
        app_module = None
    checker = getattr(app_module, "_session_valid", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    try:
        return bool(session.get("auth") and session.get("sid"))
    except Exception:
        return False


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
        resolved = _service().resolve(raw_url)
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
    "resolver_bp", "init_app",
]
