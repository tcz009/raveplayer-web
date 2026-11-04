# Source-benchmark replay fixtures

`scripts/bench_sources.py --replay auto` replays the two RaveTapePacks.com pages
below instead of contacting the live site. They were captured from the public
site through the Internet Archive because this machine's IP is currently blocked
by RaveTapePacks (TCP 443/80 time out; see `docs/PERF-SOURCES.md`).

| File | Original URL | Wayback timestamp | SHA-256 |
|---|---|---|---|
| `rtp-home.html` | `https://www.ravetapepacks.com/` | `20240809012921` | `814722bec0c36197b1149f892541a39d9c0a93e0ffe5a2a8d1baac4b7c6094d7` |
| `rtp-cat-331.html` | `https://www.ravetapepacks.com/?cat=331` | nearest 2024 snapshot | `c78bf76d96f304c20023dd1fc78c3ff9d601355003c625e731a215def6abf92a` |

Captured with `https://web.archive.org/web/<timestamp>id_/<url>` (`id_` serves
the raw page, without the Wayback toolbar).

These are third-party pages kept only as parser/timing fixtures. The home page
is 200,366 bytes with 138 parseable tapes across 17 packs and 362 category
links; the `?cat=331` page is 155,050 bytes with 48 tapes across 3 packs.

Replay is not a substitute for a live measurement: it reproduces the upstream
*bytes* and the parse cost, and adds `--latency-ms` (default 350 ms) per fetch to
stand in for the site's normal response time. It cannot reproduce TLS, redirects
or the live IP-ban behaviour.
