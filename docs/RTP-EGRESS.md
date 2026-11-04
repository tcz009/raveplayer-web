# RaveTapePacks egress via the homelab VPN

Status: implemented and verified locally; **not deployed** (web 1.6.48 and Kodi
3.9.42 are untouched in production).

## Root cause

`www.ravetapepacks.com` is firewalling the home WAN address at the **TCP
layer**, not the HTTP layer. A direct request never reaches Apache: the SYN is
dropped and `curl` ends with `code=000` after the 15 s connect timeout. That is
why changing the User-Agent, adding a Referer, clearing the cache or retrying
the page cannot recover it.

Evidence collected from the LAN and from the VPN host:

| Path | Result |
|---|---|
| Home WAN, browser UA + Referer | TCP connect timeout, no HTTP response |
| VPN host (tunnel egress), browser UA + Referer | `200 OK` in ~2.5 s |
| Public IP seen by the VPN host | different from the home WAN address (redacted) |

The block is consistent with the site's edge firewall/anti-scrape response to
the earlier request burst documented in `docs/PERF-SOURCES.md`; it is not a
hotlink (`403`) or bot-filter (`403`) response.

## Solution

A minimal HTTP `CONNECT` proxy runs on the homelab VPN host, whose default
route is the VPN tunnel. Only RaveTapePacks hostnames are allowed, and only
private/LAN clients may connect. The web app and the Kodi add-on select that
proxy **per host**: RTP requests use it, every other provider keeps the normal
direct path.

```
web core/fetch.py ─┐
web app.py stream ─┼─ CONNECT vpn-host:8888 ─→ tun0 ─→ VPN exit ─→ RTP
Kodi http.py ──────┘

archive.org, radio-browser, podcast CDNs, ... ─→ direct (unchanged)
```

The proxy is `scripts/rtp_egress_proxy.py` (stdlib only, ~260 lines). It is not
part of the web image and does not run in Docker.

## Proxy operations (VPN host)

Run it as the normal login user on a high port; no root and no firewall change
are needed on the current VPN host. The default allow-list is
`ravetapepacks.com` and the RFC1918 ranges plus loopback.

```bash
mkdir -p ~/rtp-egress
scp rtp_egress_proxy.py vpn-host:~/rtp-egress/
ssh vpn-host 'cd ~/rtp-egress && nohup python3 rtp_egress_proxy.py \
  --listen 0.0.0.0:8888 >> proxy.log 2>&1 &'
```

Keep it alive from the user crontab (the VPN host has no lingering user
manager):

```cron
* * * * * pgrep -f "[r]tp_egress_proxy.py" >/dev/null 2>&1 || nohup python3 /home/pi/rtp-egress/rtp_egress_proxy.py --listen 0.0.0.0:8888 >> /home/pi/rtp-egress/proxy.log 2>&1 &
```

Health check from the LAN:

```bash
curl -x http://vpn-host:8888 \
  -A 'Mozilla/5.0' -e 'https://www.ravetapepacks.com/' \
  -o /dev/null -w '%{http_code} %{time_total}s\n' \
  https://www.ravetapepacks.com/          # expect: 200 ...
```

A non-RTP destination is refused by the proxy (`403 host not allowed`), so it
cannot be used as a general-purpose open proxy. Logs contain the host/port of
allowed tunnels (only with `--verbose`) and denial reasons; they never contain
URLs, query strings, credentials or response bodies.

## Web configuration

| Variable | Default | Meaning |
|---|---|---|
| `RTP_EGRESS_PROXY` | empty (direct) | HTTP CONNECT proxy URL, e.g. `http://vpn-host:8888`. `host:port` is accepted. May include credentials; never logged. |
| `RTP_EGRESS_HOSTS` | `ravetapepacks.com` | Comma-separated host suffixes that use the proxy. |
| `RTP_EGRESS_FALLBACK` | `1` | On a connection-level proxy failure, retry the request directly once. Set to `0` to fail closed. |

`fetch()` and `fetch_json()` in `core/fetch.py` route through the proxy only
when the URL host matches `RTP_EGRESS_HOSTS`; every other request is unchanged.
`/api/stream` uses the same selection per redirect hop, because RTP MP3s are
served from the same blocked host. Redirects away from RTP go direct, and every
hop still passes the existing `_SafeRedirectHandler` / `_validate_redirect`
checks (scheme, host, port, and all resolved addresses).

Docker Compose forwards the variables from the host environment:

```yaml
RTP_EGRESS_PROXY: "${RTP_EGRESS_PROXY:-}"
RTP_EGRESS_HOSTS: "${RTP_EGRESS_HOSTS:-}"
```

Example deployment environment (keep real addresses out of tracked files):

```bash
RTP_EGRESS_PROXY=http://vpn-host:8888
RTP_EGRESS_HOSTS=ravetapepacks.com
RTP_EGRESS_FALLBACK=1
```

If the proxy is unreachable, the web app logs one line naming the host (never
the proxy URL) and retries directly inside the same timeout budget. The direct
path is still blocked, so users see the existing "source blocked" message
rather than a hang.

## Kodi configuration

The add-on setting is **General → "RaveTapePacks egress proxy
(host:port, blank = direct)"** (`rtp_egress_proxy` in `resources/settings.xml`).
`RTP_EGRESS_PROXY` overrides it when set, which is useful for packaged or
diagnostic runs; `RTP_EGRESS_HOSTS` and `RTP_EGRESS_FALLBACK` behave as above.

`resources/lib/http.py` applies the route to the legacy source only: page and
category fetches, `open_media()` (downloads) and `probe_media()` preserve the
caller's headers (browser UA, `Referer`, `Range`) and re-select the route after
each manual redirect. A redirect away from RTP goes direct; a redirect inside
RTP stays proxied. A dead proxy falls back to direct once per hop.

**Playback limitation:** Kodi's player fetches the MP3 URL with its own HTTP
stack, outside `resources/lib/http.py`. The add-on therefore fixes RTP
browsing/metadata and downloads, but playback still goes direct unless a local
stream bridge is added to the add-on (a separate change) or Kodi's global proxy
is pointed at the VPN host (not recommended: that is not per-host and the proxy
correctly refuses non-RTP destinations).

## Security notes

- Bound to the LAN; intended for the home network only. Never port-forward it.
- Host allow-list, `CONNECT` only, ports 443/80 only. No URL/path/query logging.
- Client allow-list defaults to loopback + RFC1918. Narrow it with
  `--allow-client` if the VPN host has other private networks.
- The app still rejects non-http(s) schemes, credentials in URLs, privileged
  ports and private/reserved DNS answers before a request is made, and
  re-validates every redirect. For a proxied hop the proxy performs the DNS
  lookup, so the proxy is the trust boundary there; it only accepts RTP hosts.
- `RTP_EGRESS_PROXY` is never written to logs, cache keys or error responses.

## Tests

```bash
# web (offline)
./.venv/bin/python -m pytest test/test_rtp_egress.py -q
./.venv/bin/python -m pytest test -q          # 493 passed

# Kodi (offline)
python3 -m unittest test.test_rtp_egress -v    # 8 tests
python3 -m unittest test.test_http test.test_legacy_streams \
  test.test_sources test.test_rtp_egress       # 76 tests OK
```

Covered: proxy selected only for RTP hosts; other hosts direct; browser
headers/Referer preserved; HTTPS `CONNECT` tunnel selected; redirect to a
private address still refused; redirect inside RTP stays proxied; proxy-down
fallback; fallback disabled; invalid/credentialed proxy config; local mock
proxy returns 200; `/api/stream` proxy selection and fallback.

The full Kodi suite currently reports 40 pre-existing failures caused by
untracked `* 2.png` / `* 2.py` duplicates in this worktree (art kebab-case and
duplicate-file checks); the HTTP/legacy suites are green.

## Live verification (local run, not deployed)

The modified web app was started on loopback with `RTP_EGRESS_PROXY` pointing
at the VPN-host proxy; production 1.6.48 was not touched.

| Check | Result |
|---|---|
| `GET /api/legacy/ravetapepacks` | `200`, pack list returned through the proxy |
| `GET /api/stream` for an RTP MP3 | `206 Partial Content`, `audio/mpeg`, non-empty audio bytes |
| Egress address | changed from the home WAN address to the VPN exit (both redacted) |
| Direct baseline | TCP timeout, confirming the block is still in place |

Exact timings are in the hand-off report; rerun the two `curl` checks above to
reproduce.

## Rollback

1. Remove `RTP_EGRESS_PROXY` from the web/Kodi environment and restart.
2. On the VPN host: remove the crontab watchdog line and `pkill -f rtp_egress_proxy.py`.

With the variable unset, both clients behave exactly as before this change
(direct only). The program itself can stay installed for later use.

## Deployment (after review)

1. Set `RTP_EGRESS_PROXY` in the host environment / Compose and rebuild the web
   container so `core/fetch.py` and `app.py` are included.
2. Confirm `/healthz` and a warm `/api/legacy/ravetapepacks` request.
3. Start the proxy before the app so the first cold fetch has an egress path.
4. Watch the app log for `RTP egress proxy unavailable`; that line means the
   proxy is down and RTP is falling back to the (still blocked) direct path.
