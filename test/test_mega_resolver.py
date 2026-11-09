# -*- coding: utf-8 -*-
"""Offline tests for :mod:`core.resolver.mega`.

The crypto vector is a real public MEGA file captured from the Kodi wiki
samples page (the "Quick Brown Fox" focus-test image).  It is embedded here so
the tests exercise the exact key derivation, AES-CBC zero-IV decryption and
NUL padding used by MEGA without making any network request.

Everything else uses a fake transport with the same signature as the real
``_post_json`` helper: ``(api_url, payload, timeout) -> (status, headers, body)``.
"""

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.resolver import mega  # noqa: E402


# ---------------------------------------------------------------------------
# Known-good public fixture (no network).  The link/key is public.
# ---------------------------------------------------------------------------
HANDLE = "raByiTib"
KEY = "Fje3cxhPb4NkBJ936KVQjPNdSwslGqiboa8IYb05t5Q"
KEY_HEX = "1637b773184f6f8364049f77e8a5508cf35d4b0b251aa89ba1af0861bd39b794"
AES_KEY_HEX = "e56afc783d55c718c5ab9716559ce718"
CONTENT_IV_HEX = "f35d4b0b251aa89b0000000000000000"
AT = (
    "YCcAoKWusVir1UMj2zXh-LDnRDMFmQI8ZtJvieJT7JVJj9fZtybnAKpQylgigoUJlnkIX5TN"
    "ZYdCiNTGm-cp8Gw4lbcp_X2O8QCVubtKZWazpo2XSiZR5aLH4pP0lX5vigMLKEjcIT6uMww"
    "YqI70fA"
)
FILENAME = "Quick Brown Fox 4K v3 (Use with 4-4-4 Chroma Subsampling).png"
THUMB = "VgwgxFcP_SkwurDGStjnawTqJ_Bn"
DOWNLOAD_URL = (
    "http://gfs208n127.userstorage.mega.co.nz/dl/"
    "5ULVebQa3z6d8gLeHy1amgO_wHs9DKisLsopUoXzKCHJIUZUyNRCQlPnGZFXUwndy5iy"
    "5-I2RZL8KAqKBH62MhLKb1QLxAgS09hInSJWlh5AgyU1p_--ZgTC3VHR-Q"
)
NEW_URL = "https://mega.nz/file/%s#%s" % (HANDLE, KEY)
OLD_URL = "https://mega.nz/#!%s!%s" % (HANDLE, KEY)


@pytest.fixture(autouse=True)
def fresh_cache():
    mega.clear_memory()
    yield
    mega.clear_memory()


def _body(g=DOWNLOAD_URL, at=AT, size=312896, **extra):
    item = {"s": size, "at": at, "g": g}
    item.update(extra)
    return json.dumps([item]).encode("utf-8")


def _fake(body=None, status=200, headers=None, calls=None):
    body = _body() if body is None else body
    headers = {} if headers is None else headers

    def transport(url, payload, timeout):
        if calls is not None:
            calls.append((url, payload, timeout))
        return status, headers, body

    return transport


def _resolve_ok(**kwargs):
    calls = []
    result = mega.resolve(
        NEW_URL, transport=_fake(calls=calls), sleep=lambda _: None, **kwargs)
    return result, calls


# ---------------------------------------------------------------------------
# URL forms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    NEW_URL,
    "https://mega.nz/file/%s/#%s" % (HANDLE, KEY),  # trailing slash in path
    "http://mega.nz/file/%s#%s" % (HANDLE, KEY),
    "https://www.mega.nz/file/%s#%s" % (HANDLE, KEY),
    "https://mega.nz/file/%s/?utm_source=x#%s" % (HANDLE, KEY),
    "https://mega.nz/file/%s#%s=" % (HANDLE, KEY),  # padded base64
    "https://mega.nz/#!%s!%s" % (HANDLE, KEY),
    "http://www.mega.co.nz/#!%s!%s" % (HANDLE, KEY),
    "https://mega.nz/#!%s!%s=" % (HANDLE, KEY),
    "mega.nz/file/%s#%s" % (HANDLE, KEY),  # scheme omitted by a DB row
])
def test_parse_supported_forms(url):
    link = mega.parse_link(url)
    assert link.handle == HANDLE
    assert link.key == KEY
    assert link.key_bytes.hex() == KEY_HEX
    assert link.cache_key() == "mega:%s:%s" % (HANDLE, KEY)
    assert link.canonical == NEW_URL
    assert str(link) == NEW_URL
    assert mega.supports(url) is True


def test_new_and_legacy_share_cache_key_and_cache():
    calls = []
    result = mega.resolve(OLD_URL, transport=_fake(calls=calls))
    assert result["source"] == "mega"
    # The same file via the modern syntax must not call the API again.
    again = mega.resolve(NEW_URL, transport=_fake(calls=calls))
    assert again["url"] == result["url"]
    assert len(calls) == 1


def test_percent_encoded_fragment_key_is_decoded():
    encoded = "".join("%%%02X" % ord(char) for char in KEY[:4]) + KEY[4:]
    link = mega.parse_link("https://mega.nz/file/%s#%s" % (HANDLE, encoded))
    assert link.key == KEY


def test_parse_accepts_parsed_link():
    link = mega.parse_link(NEW_URL)
    assert mega.parse_link(link) is link
    assert mega.supports(link) is True


@pytest.mark.parametrize("url", [
    # Host allowlist / lookalikes / private targets.
    "https://mega.nz.evil.com/file/%s#%s" % (HANDLE, KEY),
    "https://notmega.nz/file/%s#%s" % (HANDLE, KEY),
    "https://evil.com/?next=https://mega.nz/file/%s#%s" % (HANDLE, KEY),
    "http://127.0.0.1/file/%s#%s" % (HANDLE, KEY),
    "http://[::1]/file/%s#%s" % (HANDLE, KEY),
    "http://169.254.169.254/file/%s#%s" % (HANDLE, KEY),
    "https://mega.nz@evil.com/file/%s#%s" % (HANDLE, KEY),
    "https://user:pass@mega.nz/file/%s#%s" % (HANDLE, KEY),
    "https://mega.nz:8080/file/%s#%s" % (HANDLE, KEY),
    # Scheme/host shape.
    "ftp://mega.nz/file/%s#%s" % (HANDLE, KEY),
    "javascript:alert(1)",
    "not a url",
    "https://mega.nz/file/%s#%s\nX-Injected: 1" % (HANDLE, KEY),
    # Path/handle/key shape.
    "https://mega.nz/file/%s" % HANDLE,
    "https://mega.nz/file/%s#" % HANDLE,
    "https://mega.nz/file/%s#short" % HANDLE,
    "https://mega.nz/file/%s#%s" % ("short", KEY),
    "https://mega.nz/file/%s/extra#%s" % (HANDLE, KEY),
    "https://mega.nz/#!%s" % HANDLE,
    "https://mega.nz/#!%s!%s" % (HANDLE, "A" * 42),
    "https://mega.nz/file/%s#%s" % (HANDLE, "!" + KEY),
])
def test_parse_rejects_non_file_links(url):
    with pytest.raises(mega.Unsupported):
        mega.parse_link(url)
    assert mega.supports(url) is False


@pytest.mark.parametrize("url", [
    "https://mega.nz/folder/%s#%s" % (HANDLE, KEY),
    "https://mega.nz/folder/%s" % HANDLE,
    "https://mega.nz/#F!%s!%s" % (HANDLE, KEY),
    "https://mega.nz/#f!%s!%s" % (HANDLE, KEY),
])
def test_folders_are_unsupported_before_any_network(url):
    calls = []
    with pytest.raises(mega.Unsupported) as caught:
        mega.resolve(url, transport=_fake(calls=calls))
    assert "folder" in str(caught.value).lower()
    assert calls == []


def test_unsupported_never_calls_transport():
    calls = []
    with pytest.raises(mega.Unsupported):
        mega.resolve("https://example.com/file/x#y", transport=_fake(calls=calls))
    assert calls == []


# ---------------------------------------------------------------------------
# Crypto: real fixture + NIST AES-CBC known-answer vector
# ---------------------------------------------------------------------------

def test_attribute_decryption_known_vector():
    attributes = mega.decrypt_attributes(AT, KEY)
    assert attributes["n"] == FILENAME
    assert attributes["c"] == THUMB
    assert "\x00" not in attributes["n"]


def test_attribute_decryption_from_parsed_link():
    assert mega.decrypt_attributes(AT, mega.parse_link(NEW_URL))["n"] == FILENAME


def test_key_derivation_known_vector():
    aes_key, content_iv = mega.derive_content_key(KEY)
    assert aes_key.hex() == AES_KEY_HEX
    assert content_iv.hex() == CONTENT_IV_HEX
    # The attribute key and the content key are the same XOR-half value.
    assert mega.decrypt_attributes(AT, mega.parse_link(NEW_URL))["n"] == FILENAME


def test_nist_aes_cbc_known_answer():
    """NIST SP 800-38A, AES-128-CBC, first block."""
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ciphertext = bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
    plaintext = mega._aes_cbc_decrypt(key, iv, ciphertext)
    assert plaintext.hex() == "6bc1bee22e409f96e93d7e117393172a"


def test_wrong_key_is_format_changed():
    raw = bytearray(mega.parse_link(NEW_URL).key_bytes)
    raw[0] ^= 0xFF
    with pytest.raises(mega.FormatChanged):
        mega.decrypt_attributes(AT, bytes(raw))


def test_bad_attribute_base64_is_format_changed():
    with pytest.raises(mega.FormatChanged):
        mega.decrypt_attributes("!!!!", KEY)
    with pytest.raises(mega.FormatChanged):
        mega.decrypt_attributes("", KEY)


def test_attribute_ciphertext_must_be_block_aligned():
    odd = base64.urlsafe_b64encode(b"\x01" * 15).decode("ascii")
    with pytest.raises(mega.FormatChanged):
        mega.decrypt_attributes(odd, KEY)


def test_attribute_plaintext_without_mega_magic_is_format_changed():
    from Crypto.Cipher import AES

    aes_key = bytes.fromhex(AES_KEY_HEX)
    ciphertext = AES.new(aes_key, AES.MODE_CBC, b"\x00" * 16).encrypt(
        b"JUNK" + b"\x00" * 12)
    encoded = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    with pytest.raises(mega.FormatChanged):
        mega.decrypt_attributes(encoded, KEY)


def test_attribute_plaintext_with_bad_json_is_format_changed():
    from Crypto.Cipher import AES

    aes_key = bytes.fromhex(AES_KEY_HEX)
    ciphertext = AES.new(aes_key, AES.MODE_CBC, b"\x00" * 16).encrypt(
        b"MEGA{not-json" + b"\x00" * 19)
    encoded = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    with pytest.raises(mega.FormatChanged):
        mega.decrypt_attributes(encoded, KEY)


# ---------------------------------------------------------------------------
# API success and URL encoding
# ---------------------------------------------------------------------------

def test_success_contract_and_payload():
    result, calls = _resolve_ok(now=1700000000)
    assert set(result) == {"url", "headers", "expires", "source",
                           "name", "size"}
    assert result["url"] == DOWNLOAD_URL
    assert result["headers"] == {"User-Agent": mega.USER_AGENT}
    assert result["expires"] == 1700000000 + mega.URL_TTL
    assert result["source"] == "mega"
    assert result["name"] == FILENAME
    assert result["size"] == 312896

    assert len(calls) == 1
    api_url, payload, timeout = calls[0]
    assert api_url.startswith("https://g.api.mega.co.nz/cs?id=")
    assert payload == [{"a": "g", "g": 1, "p": HANDLE}]
    assert 0 < timeout <= mega.REQUEST_TIMEOUT


def test_download_url_query_and_nonce_are_preserved():
    url = "http://gfs1.userstorage.mega.co.nz/dl/token?n=abc%2Fdef&x=1"
    result = mega.resolve(
        NEW_URL, transport=_fake(body=_body(g=url)), sleep=lambda _: None)
    assert result["url"] == url


def test_download_url_html_escaping_is_unescaped():
    escaped = "http://gfs1.userstorage.mega.co.nz/dl/token?n=abc&amp;x=1"
    result = mega.resolve(
        NEW_URL, transport=_fake(body=_body(g=escaped)), sleep=lambda _: None)
    assert result["url"] == "http://gfs1.userstorage.mega.co.nz/dl/token?n=abc&x=1"


def test_fully_percent_encoded_download_url_is_unquoted():
    encoded = urllib.parse.quote(DOWNLOAD_URL, safe="")
    result = mega.resolve(
        NEW_URL, transport=_fake(body=_body(g=encoded)), sleep=lambda _: None)
    assert result["url"] == DOWNLOAD_URL


def test_mega_io_storage_host_is_allowed():
    url = "https://gfs9.userstorage.mega.io/dl/token"
    result = mega.resolve(
        NEW_URL, transport=_fake(body=_body(g=url)), sleep=lambda _: None)
    assert result["url"] == url


def test_mega_nz_storage_host_is_allowed():
    url = "https://gfs9.userstorage.mega.nz/dl/token"
    result = mega.resolve(
        NEW_URL, transport=_fake(body=_body(g=url)), sleep=lambda _: None)
    assert result["url"] == url


@pytest.mark.parametrize("download_url", [
    "http://127.0.0.1/dl/x",                       # loopback
    "http://169.254.169.254/latest/meta-data",     # link-local metadata
    "http://[::1]/dl/x",                           # IPv6 loopback
    "http://localhost/dl/x",                       # non-MEGA host
    "http://gfs1.userstorage.mega.co.nz.evil.com/dl/x",
    "http://eviluserstorage.mega.co.nz/dl/x",
    "http://userstorage.mega.co.nz@evil.com/dl/x", # credentials, evil host
    "http://user:pass@gfs1.userstorage.mega.co.nz/dl/x",
    "https://mega.nz/dl/x",                        # link host, not storage
    "ftp://gfs1.userstorage.mega.co.nz/dl/x",
    "javascript:alert(1)",
    "http://gfs1.userstorage.mega.co.nz/dl/x#frag",
    "http://gfs1.userstorage.mega.co.nz:99999/dl/x",
])
def test_download_url_allowlist_and_private_host_rejection(download_url):
    with pytest.raises(mega.FormatChanged):
        mega.resolve(
            NEW_URL, transport=_fake(body=_body(g=download_url)),
            sleep=lambda _: None)


def test_bad_link_key_fails_attribute_decryption():
    other_key = base64.urlsafe_b64encode(b"\x01" * 32).decode("ascii").rstrip("=")
    with pytest.raises(mega.FormatChanged):
        mega.resolve(
            "https://mega.nz/file/%s#%s" % (HANDLE, other_key),
            transport=_fake(), sleep=lambda _: None)


# ---------------------------------------------------------------------------
# API error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error,error_cls", [
    (-9, mega.NotFound),       # ENOENT
    (-8, mega.NotFound),       # EEXPIRED
    (-11, mega.NotFound),      # EACCESS
    (-16, mega.NotFound),      # EBLOCKED
    (-2, mega.FormatChanged),  # EARGS
    (-14, mega.FormatChanged), # EKEY
    (-99, mega.FormatChanged), # unknown
    (-1, mega.UpstreamError),  # EINTERNAL
])
def test_json_error_mapping(error, error_cls):
    body = json.dumps([{"error": error}]).encode("utf-8")
    with pytest.raises(error_cls):
        mega.resolve(NEW_URL, transport=_fake(body=body), sleep=lambda _: None)


@pytest.mark.parametrize("status,body,error_cls", [
    (404, b"", mega.NotFound),
    (404, b"<html>gone</html>", mega.NotFound),
    (403, b"", mega.NotFound),
    (410, b"", mega.NotFound),
    (500, b"", mega.UpstreamError),
    (509, b"<html>busy</html>", mega.RateLimited),
    (502, b"", mega.UpstreamError),
    (302, b"", mega.FormatChanged),
    (200, b"not json", mega.FormatChanged),
    (200, b"", mega.FormatChanged),
    (200, b"[]", mega.FormatChanged),
    (200, b'[{"s":1,"at":"%s"}]' % AT.encode("ascii"), mega.FormatChanged),
    (200, b'[{"s":1,"g":"%s"}]' % DOWNLOAD_URL.encode("ascii"), mega.FormatChanged),
    (200, b'[{"s":1,"g":1,"at":"%s"}]' % AT.encode("ascii"), mega.FormatChanged),
    (200, b'[{"s":"x","g":"%s","at":"%s"}]' % (
        DOWNLOAD_URL.encode("ascii"), AT.encode("ascii")), mega.FormatChanged),
])
def test_http_status_and_shape_mapping(status, body, error_cls):
    with pytest.raises(error_cls):
        mega.resolve(
            NEW_URL, transport=_fake(body=body, status=status),
            sleep=lambda _: None)


def test_eagain_retries_with_exponential_backoff():
    calls = []
    sleeps = []
    responses = [
        (509, {}, b""),
        (200, {}, b'[{"error":-3}]'),
        (200, {}, _body()),
    ]

    def transport(url, payload, timeout):
        calls.append((url, payload))
        return responses[len(calls) - 1]

    result = mega.resolve(NEW_URL, transport=transport, sleep=sleeps.append)
    assert result["url"] == DOWNLOAD_URL
    assert len(calls) == 3
    assert sleeps == [mega.BACKOFF_BASE, mega.BACKOFF_BASE * 2]


def test_rate_limit_is_bounded_and_negative_cached():
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 509, {}, b""

    with pytest.raises(mega.RateLimited) as caught:
        mega.resolve(NEW_URL, transport=transport, sleep=lambda _: None)
    assert calls == [1, 1, 1]
    assert caught.value.retryable is True
    # A second resolve must come from the negative cache, not MEGA.
    with pytest.raises(mega.RateLimited):
        mega.resolve(NEW_URL, transport=transport, sleep=lambda _: None)
    assert calls == [1, 1, 1]


def test_retry_after_header_is_honoured():
    calls = []
    sleeps = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 509, {"Retry-After": "2"}, b""

    with pytest.raises(mega.RateLimited):
        mega.resolve(NEW_URL, transport=transport, sleep=sleeps.append)
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]


def test_quota_is_rate_limited_but_not_busy_retried():
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 200, {}, b'[{"error":-17}]'

    with pytest.raises(mega.RateLimited) as caught:
        mega.resolve(NEW_URL, transport=transport, sleep=lambda _: None)
    assert calls == [1]
    assert caught.value.retryable is False
    assert caught.value.retry_after == 300


def test_network_failure_becomes_upstream_error(monkeypatch):
    class Boom(object):
        def open(self, *args, **kwargs):
            raise urllib.error.URLError("no route")

    monkeypatch.setattr(mega, "_opener", lambda: Boom())
    with pytest.raises(mega.UpstreamError):
        mega._post_json("https://g.api.mega.co.nz/cs?id=1", [], 1.0)


def test_transport_upstream_error_is_not_cached():
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        raise mega.UpstreamError("boom")

    for _ in range(2):
        with pytest.raises(mega.UpstreamError):
            mega.resolve(NEW_URL, transport=transport, sleep=lambda _: None)
    assert calls == [1, 1]


# ---------------------------------------------------------------------------
# Cache, TTL and single-flight
# ---------------------------------------------------------------------------

def test_cache_reuses_url_and_expiry_and_copies_values():
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 200, {}, _body()

    first = mega.resolve(NEW_URL, transport=transport)
    second = mega.resolve(NEW_URL, transport=transport)
    assert calls == [1]
    assert first == second

    # A caller mutating its result must not poison the cache.
    first["url"] = "http://evil.example/x"
    first["headers"]["X-Test"] = "1"
    third = mega.resolve(NEW_URL, transport=transport)
    assert third["url"] == DOWNLOAD_URL
    assert "X-Test" not in third["headers"]
    assert calls == [1]


def test_cache_expiry_triggers_a_new_api_call(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mega, "_monotonic", lambda: clock[0])
    calls = []
    urls = [DOWNLOAD_URL + "?v=1", DOWNLOAD_URL + "?v=2"]

    def transport(url, payload, timeout):
        calls.append(1)
        return 200, {}, _body(g=urls[len(calls) - 1])

    first = mega.resolve(NEW_URL, transport=transport)
    assert first["url"] == urls[0]
    clock[0] += mega.URL_TTL - 1
    assert mega.resolve(NEW_URL, transport=transport)["url"] == urls[0]
    assert calls == [1]
    clock[0] += 2
    assert mega.resolve(NEW_URL, transport=transport)["url"] == urls[1]
    assert calls == [1, 1]


def test_negative_cache_reuses_not_found_and_expires(monkeypatch):
    clock = [2000.0]
    monkeypatch.setattr(mega, "_monotonic", lambda: clock[0])
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 200, {}, b'[{"error":-9}]'

    with pytest.raises(mega.NotFound):
        mega.resolve(NEW_URL, transport=transport)
    with pytest.raises(mega.NotFound):
        mega.resolve(NEW_URL, transport=transport)
    assert calls == [1]

    clock[0] += mega.NEGATIVE_TTL + 1
    with pytest.raises(mega.NotFound):
        mega.resolve(NEW_URL, transport=transport)
    assert calls == [1, 1]


def test_format_changed_is_not_negative_cached():
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 200, {}, b"not json"

    for _ in range(2):
        with pytest.raises(mega.FormatChanged):
            mega.resolve(NEW_URL, transport=transport, sleep=lambda _: None)
    assert calls == [1, 1]


def test_invalidate_drops_only_one_entry():
    calls = []

    def transport(url, payload, timeout):
        calls.append(1)
        return 200, {}, _body()

    mega.resolve(NEW_URL, transport=transport)
    assert mega.invalidate(OLD_URL) is True
    assert mega.invalidate(OLD_URL) is False
    mega.resolve(OLD_URL, transport=transport)
    assert calls == [1, 1]


def test_different_handles_use_different_cache_entries():
    calls = []

    def transport(url, payload, timeout):
        calls.append(payload[0]["p"])
        return 200, {}, _body()

    mega.resolve(NEW_URL, transport=transport)
    mega.resolve("https://mega.nz/file/AAAAAAAA#%s" % KEY, transport=transport)
    assert calls == [HANDLE, "AAAAAAAA"]


def test_single_flight_coalesces_concurrent_resolves():
    calls = []
    started = threading.Event()
    release = threading.Event()

    def transport(url, payload, timeout):
        calls.append(1)
        started.set()
        assert release.wait(5.0)
        return 200, {}, _body()

    results = []
    errors = []

    def worker():
        try:
            results.append(mega.resolve(NEW_URL, transport=transport))
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    assert started.wait(2.0)
    time.sleep(0.15)  # let the other workers join the in-flight request
    release.set()
    for thread in threads:
        thread.join(5.0)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert calls == [1]
    assert len(results) == 6
    assert all(result == results[0] for result in results)
    assert results[0]["url"] == DOWNLOAD_URL


def test_single_flight_shares_negative_result():
    calls = []
    started = threading.Event()
    release = threading.Event()

    def transport(url, payload, timeout):
        calls.append(1)
        started.set()
        assert release.wait(5.0)
        return 200, {}, b'[{"error":-9}]'

    errors = []

    def worker():
        try:
            mega.resolve(NEW_URL, transport=transport)
        except mega.NotFound as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert started.wait(2.0)
    time.sleep(0.15)
    release.set()
    for thread in threads:
        thread.join(5.0)

    assert calls == [1]
    assert len(errors) == 4
    assert all(isinstance(exc, mega.NotFound) for exc in errors)
