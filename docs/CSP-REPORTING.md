# CSP reporting — 1.6.24

The enforcing CSP is **unchanged** in this release. In addition, HTML responses
carry a report-only copy so real browser violations can be collected before any
tightening.

## Headers

When `RAVEWEB_CSP_REPORT_ONLY` is unset/`1` (default), HTML responses include:

```
Content-Security-Policy: <the existing enforcing policy>
Content-Security-Policy-Report-Only: <the existing policy>;
  report-uri /api/csp-report; report-to csp-endpoint
Report-To: {"group":"csp-endpoint","max_age":10886400,
            "endpoints":[{"url":"https://<host>/api/csp-report"}]}
```

Set `RAVEWEB_CSP_REPORT_ONLY=0` to remove only the report-only header; the
enforcing header stays. `/api/csp-report` is public (browsers post without the
app session), has a 64 KiB body cap, parses both the legacy `csp-report` shape
and Reporting API arrays, logs one sanitized line per report
(`csp violation directive=… blocked=… document=… disposition=…`) and returns
`204`. It never stores the request body or URL in the audit database.

## Promoting a rule to enforcement

1. Deploy/collect for at least a few days; watch:
   `docker logs raveplayer-web 2>&1 | grep 'csp violation'`.
2. Identify a directive that is consistently violated by code you control (not
   by third-party extensions or Safari reader modes).
3. Change the enforcing `CSP` string in `app.py` to add the tightened directive
   **after** the report-only evidence shows no legitimate breakage; rebuild.
4. Keep the report-only copy for a release, then either remove it
   (`RAVEWEB_CSP_REPORT_ONLY=0`) or leave it as monitoring.
5. Browser-check the affected flows (Mixcloud/SoundCloud frames, player,
   downloads, login) — the offline suite only asserts headers.

Do **not** add `'unsafe-inline'`, `'unsafe-eval'`, wildcard `script-src`, or new
framed hosts just to silence a report; those are deliberate CSP changes.
