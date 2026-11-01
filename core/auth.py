# -*- coding: utf-8 -*-
"""Single-operator web login: password hashing, config loading, throttling.

Accounts/OAuth are deliberately out of scope for 1.6.20.  One operator sets a
password hash (or a first-run plaintext that is hashed in memory) plus a secret
key, and every page/API request except the login surface and PWA/health assets
requires a signed session.

Supported hash formats:
  * Argon2 (``$argon2id$...``) via the ``argon2-cffi`` package;
  * bcrypt (``$2a$/$2b$/$2y$``) via the ``bcrypt`` package;
  * Werkzeug ``scrypt:`` / ``pbkdf2:`` (stdlib fallback, always available).
"""

import hmac
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass

try:  # optional, recommended
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHash, VerificationError
    _ARGON2 = PasswordHasher()
except ImportError:  # pragma: no cover - depends on the install extras
    PasswordHasher = None
    InvalidHash = VerificationError = Exception
    _ARGON2 = None

try:  # optional, recommended
    import bcrypt
except ImportError:  # pragma: no cover - depends on the install extras
    bcrypt = None

from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_SECRETS = frozenset((
    "", "secret", "change-me", "changeme", "dev", "development", "test",
    "raveplayer", "rave-player", "password", "replace-me", "please-change",
))
MIN_PASSWORD_LENGTH = 8


class AuthConfigError(RuntimeError):
    """Authentication is misconfigured; the app refuses to start."""


@dataclass
class AuthConfig:
    password_hash: str              # owner (admin)
    friends_hash: str               # friends (viewer), empty = disabled
    secret_key: str
    session_hours: float = 12.0
    remember_days: float = 30.0
    max_failures: int = 5
    failure_window: float = 600.0
    lockout_seconds: float = 900.0
    production: bool = False
    generated_hash: str = ""
    generated_friends_hash: str = ""


def _env_float(env, name, default, minimum=0.0):
    try:
        value = float(env.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value):
        return float(default)
    return max(minimum, value)


def _env_int(env, name, default, minimum=1):
    try:
        value = int(env.get(name, "") or default)
    except (TypeError, ValueError):
        return int(default)
    return max(minimum, value)


def is_production(env=None):
    env = os.environ if env is None else env
    mode = (env.get("RAVEWEB_ENV") or env.get("FLASK_ENV") or "").strip().lower()
    return bool(env.get("RAVEWEB_PROD") == "1" or mode in ("prod", "production"))


def hash_scheme(encoded):
    if not isinstance(encoded, str) or not encoded:
        return ""
    if encoded.startswith("$argon2"):
        return "argon2"
    if encoded.startswith(("$2a$", "$2b$", "$2y$")):
        return "bcrypt"
    if encoded.startswith(("scrypt:", "pbkdf2:")):
        return "werkzeug"
    return ""


def validate_scheme(encoded):
    scheme = hash_scheme(encoded)
    if scheme == "argon2" and _ARGON2 is None:
        raise AuthConfigError(
            "RAVEWEB_PASSWORD_HASH is Argon2 but argon2-cffi is not installed")
    if scheme == "bcrypt" and bcrypt is None:
        raise AuthConfigError(
            "RAVEWEB_PASSWORD_HASH is bcrypt but bcrypt is not installed")
    if not scheme:
        raise AuthConfigError(
            "RAVEWEB_PASSWORD_HASH must be an Argon2, bcrypt or Werkzeug "
            "(scrypt:/pbkdf2:) hash")
    return scheme


def generate_hash(password):
    """Hash a plaintext password with the best available scheme."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthConfigError(
            "RAVEWEB_PASSWORD must be at least %d characters" % MIN_PASSWORD_LENGTH)
    if _ARGON2 is not None:
        return _ARGON2.hash(password)
    if bcrypt is not None:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    try:
        return generate_password_hash(password, method="scrypt")
    except (ValueError, TypeError, AttributeError):  # old OpenSSL: no scrypt
        return generate_password_hash(password, method="pbkdf2:sha256:600000")


def verify_password(encoded, password):
    """Constant-time-ish verification across the supported schemes."""
    if not isinstance(encoded, str) or not encoded or not isinstance(password, str):
        return False
    scheme = hash_scheme(encoded)
    try:
        if scheme == "argon2":
            if _ARGON2 is None:
                return False
            return bool(_ARGON2.verify(encoded, password))
        if scheme == "bcrypt":
            if bcrypt is None:
                return False
            return bool(bcrypt.checkpw(password.encode("utf-8")[:72],
                                       encoded.encode("ascii")))
        if scheme == "werkzeug":
            return bool(check_password_hash(encoded, password))
    except Exception:  # noqa: BLE001 - a malformed hash is just a failed login
        return False
    return False


def load_config(env=None):
    env = os.environ if env is None else env
    encoded = (env.get("RAVEWEB_PASSWORD_HASH") or "").strip()
    generated = ""
    if not encoded:
        plain = env.get("RAVEWEB_PASSWORD") or ""
        if not plain:
            raise AuthConfigError(
                "set RAVEWEB_PASSWORD_HASH (preferred) or RAVEWEB_PASSWORD; "
                "refusing to start without an owner password")
        encoded = generate_hash(plain)
        generated = encoded
    validate_scheme(encoded)

    friends = (env.get("RAVEWEB_FRIENDS_PASSWORD_HASH") or "").strip()
    generated_friends = ""
    if not friends:
        friends_plain = env.get("RAVEWEB_FRIENDS_PASSWORD") or ""
        if friends_plain:
            friends = generate_hash(friends_plain)
            generated_friends = friends
    if friends:
        validate_scheme(friends)

    production = is_production(env)
    secret = (env.get("SECRET_KEY") or "").strip()
    if production:
        if not secret:
            raise AuthConfigError("SECRET_KEY must be set in production")
        if secret in DEFAULT_SECRETS:
            raise AuthConfigError("SECRET_KEY is a default/placeholder value")
    elif not secret:
        # Ephemeral dev/test key: sessions do not survive a restart, but the app
        # can boot.  Production always requires an explicit key.
        secret = secrets.token_hex(32)

    return AuthConfig(
        password_hash=encoded,
        friends_hash=friends,
        secret_key=secret,
        session_hours=_env_float(env, "RAVEWEB_SESSION_HOURS", 12.0, 0.05),
        remember_days=_env_float(env, "RAVEWEB_REMEMBER_DAYS", 30.0, 0.05),
        max_failures=_env_int(env, "RAVEWEB_LOGIN_MAX_FAILURES", 5),
        failure_window=_env_float(env, "RAVEWEB_LOGIN_FAILURE_WINDOW", 600.0, 1.0),
        lockout_seconds=_env_float(env, "RAVEWEB_LOGIN_LOCKOUT", 900.0, 1.0),
        production=production,
        generated_hash=generated,
        generated_friends_hash=generated_friends,
    )


def same_text(left, right):
    """Constant-time comparison for usernames/tokens (both str or bytes)."""
    try:
        left = left.encode("utf-8") if isinstance(left, str) else left
        right = right.encode("utf-8") if isinstance(right, str) else right
        return hmac.compare_digest(left or b"", right or b"")
    except (TypeError, ValueError):
        return False


class LoginLimiter(object):
    """In-process failed-login throttle with a temporary lockout."""

    def __init__(self, max_failures=5, failure_window=600.0, lockout_seconds=900.0):
        self.max_failures = max(1, int(max_failures))
        self.failure_window = float(failure_window)
        self.lockout_seconds = float(lockout_seconds)
        self._lock = threading.Lock()
        self._state = {}  # ip -> [window_start, failures, locked_until]

    def _now(self):
        return time.monotonic()

    def retry_after(self, ip):
        """Seconds until ``ip`` may try again, or 0 when it is not locked."""
        now = self._now()
        with self._lock:
            state = self._state.get(ip)
            if not state:
                return 0
            window_start, failures, locked_until = state
            if locked_until and locked_until > now:
                return max(1, int(math.ceil(locked_until - now)))
            if now - window_start > self.failure_window:
                self._state.pop(ip, None)
            return 0

    def fail(self, ip):
        now = self._now()
        with self._lock:
            window_start, failures, locked_until = self._state.get(
                ip, [now, 0, 0.0])
            if now - window_start > self.failure_window:
                window_start, failures = now, 0
            failures += 1
            locked_until = (now + self.lockout_seconds
                            if failures >= self.max_failures else 0.0)
            self._state[ip] = [window_start, failures, locked_until]
            return failures

    def reset(self, ip):
        with self._lock:
            self._state.pop(ip, None)

    def clear(self):
        with self._lock:
            self._state.clear()
