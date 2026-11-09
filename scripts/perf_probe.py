# -*- coding: utf-8 -*-
"""Black-box HTTP benchmark for the Rave Player web app.

Logs in as the owner (the API has required a session since 1.6.21) and measures
each endpoint the way a browser pays for it: a fresh TCP connection per sample,
time to first body byte (TTFB) and total time, wire bytes, gzip size and the
decoded JSON size.  The companion ``perf_providers.py`` attributes the same
routes to provider vs local work; the server's own ``req ... ms=`` line (when
INFO logging is enabled) gives the handler time.

    RAVEWEB_TEST_PASSWORD=... .venv/bin/python scripts/perf_probe.py \
        --base http://127.0.0.1:8098 --samples 20 --out docs/bench/perf.json

    # against the live NAS (owner login, read-only GETs)
    RAVEWEB_TEST_PASSWORD=... .venv/bin/python scripts/perf_probe.py \
        --base http://192.168.1.179:8097 --samples 10

Nothing is written to the server.  Stream endpoints are sampled with a Range
request and the connection is closed after the first chunk, so a multi-hundred
MB set is never downloaded.
"""

import argparse
import gzip
import http.client
import json
import os
import re
import statistics
import sys
import time
import urllib.parse

USER_AGENT = "RavePlayerPerfProbe/1.0"
STREAM_BYTES = 64 * 1024

# (label, path, kind) - kind "json" | "html" | "static" | "stream"
ENDPOINTS = [
    ("health", "/healthz", "json"),
    ("shell /", "/", "html"),
    ("menu", "/api/menu", "json"),
    ("latest", "/api/latest", "json"),
    ("ukraves p1", "/api/ukraves?page=1", "json"),
    ("ukraves p2", "/api/ukraves?page=2", "json"),
    ("ukraves p3", "/api/ukraves?page=3", "json"),
    ("ezgarage", "/api/ezgarage", "json"),
    ("mikus", "/api/mikus", "json"),
    ("downloads", "/api/downloads", "json"),
    ("rtp hub", "/api/legacy/ravetapepacks", "json"),
    ("rtp events 331", "/api/legacy/ravetapepacks/index/events/331", "json"),
    ("rtp djs 106", "/api/legacy/ravetapepacks/index/djs/106", "json"),
    ("rtp all", "/api/legacy/ravetapepacks/all", "json"),
    ("static app.js", "/static/app.js", "static"),
    ("static app.css", "/static/app.css", "static"),
]


def _connection(base):
    parts = urllib.parse.urlsplit(base)
    if parts.scheme == "https":
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443,
                                           timeout=180)
    return http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                      timeout=180)


def _request(base, path, cookie="", extra_headers=None, read_all=True,
             body_limit=None):
    """One request on a fresh connection.  Returns a result dict."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
    }
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    conn = _connection(base)
    started = time.perf_counter()
    result = {"path": path, "status": 0, "ttfb_ms": 0.0, "total_ms": 0.0,
              "wire_bytes": 0, "decoded_bytes": 0, "encoding": "",
              "error": ""}
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        result["status"] = int(response.status)
        result["encoding"] = response.getheader("Content-Encoding") or ""
        first = response.read(1)
        result["ttfb_ms"] = (time.perf_counter() - started) * 1000.0
        chunks = [first] if first else []
        received = len(first or b"")
        limit = body_limit if body_limit is not None else 8 * 1024 * 1024
        while received < limit:
            chunk = response.read(min(65536, limit - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        body = b"".join(chunks)
        result["wire_bytes"] = received
        if result["encoding"] == "gzip" and body:
            try:
                result["decoded_bytes"] = len(gzip.decompress(body))
            except (OSError, EOFError):
                result["decoded_bytes"] = 0
        else:
            result["decoded_bytes"] = len(body)
    except Exception as exc:  # noqa: BLE001 - a probe must report, not crash
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["total_ms"] = (time.perf_counter() - started) * 1000.0
        return result
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    result["total_ms"] = (time.perf_counter() - started) * 1000.0
    return result


def login(base, username, password):
    """Log in and return the Cookie header value, or raise."""
    conn = _connection(base)
    try:
        conn.request("GET", "/login", headers={"User-Agent": USER_AGENT})
        response = conn.getresponse()
        html = response.read().decode("utf-8", "replace")
        cookies = response.getheader("Set-Cookie") or ""
    finally:
        conn.close()
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    if not match:
        raise SystemExit("login page did not contain a CSRF field (status?)")
    csrf = match.group(1)
    token = ""
    for part in cookies.split(","):
        name, _, value = part.split(";", 1)[0].strip().partition("=")
        if name == "raveplayer_session":
            token = value
    if not token:
        raise SystemExit("login page did not set a session cookie")
    body = urllib.parse.urlencode({
        "username": username, "password": password, "csrf": csrf})
    conn = _connection(base)
    try:
        conn.request("POST", "/login", body=body, headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "raveplayer_session=" + token,
            "Origin": base,
        })
        response = conn.getresponse()
        response.read()
        status = response.status
        cookie_header = response.getheader("Set-Cookie") or ""
    finally:
        conn.close()
    if status not in (302, 303):
        raise SystemExit("login failed with HTTP %s" % status)
    for part in cookie_header.split(","):
        name, _, value = part.split(";", 1)[0].strip().partition("=")
        if name == "raveplayer_session" and value:
            token = value
    return "raveplayer_session=" + token


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _summary(results):
    ok = [row for row in results if not row["error"] and row["status"] < 500]
    if not ok:
        return {"n": 0, "error": results[0]["error"] if results else "no samples"}
    ttfb = [row["ttfb_ms"] for row in ok]
    total = [row["total_ms"] for row in ok]
    wire = [row["wire_bytes"] for row in ok]
    return {
        "n": len(ok),
        "status": ok[0]["status"],
        "p50_ms": round(statistics.median(total), 1),
        "p95_ms": round(_percentile(total, 0.95), 1),
        "max_ms": round(max(total), 1),
        "ttfb_p50_ms": round(statistics.median(ttfb), 1),
        "ttfb_p95_ms": round(_percentile(ttfb, 0.95), 1),
        "wire_bytes_p50": int(statistics.median(wire)),
        "decoded_bytes_p50": int(statistics.median(
            [row["decoded_bytes"] for row in ok])),
        "encoding": ok[0]["encoding"],
        "errors": len(results) - len(ok),
    }


def discover(base, cookie):
    """Pick one real id for the expensive stream routes (read-only GETs)."""
    found = {}
    try:
        page = _request(base, "/api/ukraves?page=1", cookie)
        if page["status"] == 200 and page["wire_bytes"]:
            data = json.loads(_body_of(base, "/api/ukraves?page=1", cookie))
            for item in data.get("items") or []:
                download = item.get("download") or {}
                if download.get("source") == "ukraves" and download.get("id"):
                    found["ukraves"] = download["id"]
                    break
    except Exception:  # noqa: BLE001
        pass
    try:
        data = json.loads(_body_of(base, "/api/ezgarage", cookie))
        for item in data.get("items") or []:
            download = item.get("download") or {}
            if download.get("id"):
                found["ezgarage"] = download["id"]
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        data = json.loads(_body_of(base, "/api/mikus?page=1", cookie))
        for item in data.get("items") or []:
            detail = item.get("open") or ""
            if detail:
                detail_data = json.loads(_body_of(base, detail, cookie))
                for row in detail_data.get("items") or []:
                    download = row.get("download") or {}
                    if download.get("source") == "mikus" and download.get("id"):
                        found["mikus"] = download["id"]
                        break
            if "mikus" in found:
                break
    except Exception:  # noqa: BLE001
        pass
    return found


def _body_of(base, path, cookie):
    conn = _connection(base)
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT,
                                           "Cookie": cookie,
                                           "Accept-Encoding": "gzip"})
        response = conn.getresponse()
        raw = response.read()
        if (response.getheader("Content-Encoding") or "") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8098")
    parser.add_argument("--username", default="owner")
    parser.add_argument("--password-env", default="RAVEWEB_TEST_PASSWORD")
    parser.add_argument("--password", default="")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--only", default="", help="comma-separated label substrings")
    parser.add_argument("--streams", action="store_true",
                        help="include range-sampled stream routes (cold resolve)")
    parser.add_argument("--out", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    password = args.password or os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit("set %s or --password" % args.password_env)
    cookie = login(args.base, args.username, password)

    endpoints = list(ENDPOINTS)
    if args.streams:
        ids = discover(args.base, cookie)
        if ids.get("ukraves"):
            endpoints.append(("ukraves stream", "/api/ukraves/%s/stream" % ids["ukraves"], "stream"))
        if ids.get("ezgarage"):
            endpoints.append(("ezgarage stream", "/api/ezgarage/%s/stream" % ids["ezgarage"], "stream"))
        if ids.get("mikus"):
            endpoints.append(("mikus drive", "/api/mikus/drive/%s" % ids["mikus"], "json"))
            endpoints.append(("mikus drive stream", "/api/mikus/drive/%s/stream" % ids["mikus"], "stream"))
        print("discovered ids: %s" % {k: v[:8] + "…" for k, v in ids.items()},
              file=sys.stderr)

    if args.only:
        wanted = [part.strip().lower() for part in args.only.split(",") if part.strip()]
        endpoints = [row for row in endpoints
                     if any(part in row[0].lower() for part in wanted)]

    report = {"base": args.base, "label": args.label,
              "samples": args.samples, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "endpoints": {}}
    for label, path, kind in endpoints:
        for _ in range(args.warmup):
            _request(args.base, path, cookie, body_limit=1024 * 1024)
        results = []
        for _ in range(args.samples):
            extra = {"Range": "bytes=0-%d" % (STREAM_BYTES - 1)} if kind == "stream" else None
            results.append(_request(args.base, path, cookie, extra_headers=extra,
                                    body_limit=(STREAM_BYTES if kind == "stream"
                                                else 8 * 1024 * 1024)))
        summary = _summary(results)
        report["endpoints"][label] = dict(summary, path=path, kind=kind)
        print("%-20s %-46s n=%2d p50=%8.1f p95=%8.1f ttfb95=%8.1f %8d B %s"
              % (label, path, summary.get("n", 0), summary.get("p50_ms", 0),
                 summary.get("p95_ms", 0), summary.get("ttfb_p95_ms", 0),
                 summary.get("wire_bytes_p50", 0), summary.get("encoding", "")),
              file=sys.stderr)

    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print("wrote %s" % args.out, file=sys.stderr)
    else:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
