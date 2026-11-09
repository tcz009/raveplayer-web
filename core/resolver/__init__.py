# -*- coding: utf-8 -*-
"""Rave resolver package.

Importing :mod:`core.resolver` stays Flask-free so the Kodi/web provider
modules can share the URL-safety helpers without pulling in the HTTP service.
The service (and its Flask blueprint) is loaded lazily on first access::

    from core.resolver import Resolver, Resolved, DirectResolver
    from core.resolver.service import resolver_bp, ResolverService

Adapters implemented by other agents live in sibling modules
(``mediafire.py``, ``drive.py``, ``mega.py``) and implement the same
:class:`~core.resolver.base.Resolver` protocol.
"""

from .base import (
    AUDIO_SUFFIXES,
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    MAX_URL_LENGTH,
    MIN_TIMEOUT,
    BlockedURL,
    HostAllowlist,
    HTTPClient,
    HTTPResponse,
    InvalidRequest,
    NotFound,
    RateLimited,
    Resolved,
    Resolver,
    ResolverError,
    Unauthorized,
    UnknownURL,
    UnsupportedMedia,
    UpstreamError,
    UpstreamTimeout,
    assert_public_url,
    check_public_host,
    clamp_timeout,
    env_hosts,
    is_audio_url,
    is_private_hostname,
    normalize_url,
    registrable_domain,
    sanitize_headers,
    split_url,
    url_host,
    validate_resolved,
)
from .cache import CacheEntry, ResolverCache, cacheable_error
from .direct import DEFAULT_DIRECT_HOSTS, DirectResolver
from .podomatic import PODOMATIC_HOSTS, PodomaticResolver

__version__ = "1.0.0"

_SERVICE_EXPORTS = frozenset((
    "ResolverService", "TokenBucket", "build_default_service",
    "resolver_bp", "init_app",
))

__all__ = [
    "AUDIO_SUFFIXES", "DEFAULT_TIMEOUT", "MAX_TIMEOUT", "MAX_URL_LENGTH",
    "MIN_TIMEOUT", "BlockedURL", "HostAllowlist", "HTTPClient",
    "HTTPResponse", "InvalidRequest", "NotFound", "RateLimited", "Resolved",
    "Resolver", "ResolverError", "Unauthorized", "UnknownURL",
    "UnsupportedMedia", "UpstreamError", "UpstreamTimeout",
    "assert_public_url", "check_public_host", "clamp_timeout", "env_hosts",
    "is_audio_url", "is_private_hostname", "normalize_url",
    "registrable_domain", "sanitize_headers", "split_url", "url_host", "validate_resolved",
    "CacheEntry", "ResolverCache", "cacheable_error",
    "DEFAULT_DIRECT_HOSTS", "DirectResolver", "PODOMATIC_HOSTS",
    "PodomaticResolver", "ResolverService", "TokenBucket",
    "build_default_service", "resolver_bp", "init_app",
]


def __getattr__(name):
    # Lazy Flask import: `from core.resolver import resolver_bp` still works,
    # but the provider/Kodi path never imports Flask just for URL helpers.
    if name in _SERVICE_EXPORTS:
        from . import service as _service

        return getattr(_service, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
