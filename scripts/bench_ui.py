# -*- coding: utf-8 -*-
"""Rail-first-paint / layout-shift benchmark (web 1.6.18 perf pass).

Starts the real app on localhost with provider stubs and drives it in headless
Chrome (system Chrome via RAVEWEB_CHROMIUM_EXECUTABLE).  Reports:
  * rail interactive (first nav button visible, from navigation start)
  * first list row (Latest)
  * cumulative layout shift (CLS) over the first 1.5 s
Cold = cache dir cleared first; warm = second/third runs.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = tempfile.mkdtemp(prefix="raveweb-ui-bench-")
os.environ.setdefault("RAVEWEB_DATA", BASE)
os.environ.setdefault("RAVEPLAYER_CACHE_DIR", os.path.join(BASE, "cache"))
os.environ.setdefault("RAVEPLAYER_FAV_DIR", os.path.join(BASE, "favs"))
os.environ.setdefault("SECRET_KEY", "bench-secret-key")
os.environ.setdefault("RAVEWEB_PASSWORD", "bench-password")

import app as app_module  # noqa: E402
from core import fetch as core_fetch  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

CHROME = os.environ.get("RAVEWEB_CHROMIUM_EXECUTABLE", "")
NO_SANDBOX = os.environ.get("RAVEWEB_PLAYWRIGHT_NO_SANDBOX") == "1"

INIT = """() => {
    window.__bench = {start: performance.now(), cls: 0, shifts: []};
    try {
        new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) {
                if (entry.hadRecentInput) continue;
                window.__bench.cls += entry.value;
                window.__bench.shifts.push({
                    value: Math.round(entry.value * 10000) / 10000,
                    sources: (entry.sources || []).slice(0, 3).map((source) => {
                        const node = source.node;
                        return (node && (node.id || node.className || node.nodeName)) || "?";
                    }),
                });
            }
        }).observe({type: "layout-shift", buffered: true});
    } catch (_) {}
}"""


def fake_latest(limit=60):
    return [{"kind": "pack", "title": "Bench Pack", "identifier": "pack-0",
             "source": "Rave Tape Packs", "when": None},
            {"kind": "episode", "title": "Bench Episode", "url": "https://cdn.example/a.mp3",
             "source": "Bench Feed", "when": None}]


def fake_search(query, page=1, rows=50, sort="downloads desc"):
    return [{"identifier": "pack-0", "title": "Bench Pack", "year": "1993"}], 1


def main():
    from playwright.sync_api import sync_playwright

    patcher = __import__("pytest").MonkeyPatch()
    patcher.setattr(app_module.latest, "merge", fake_latest)
    patcher.setattr(app_module.archive, "search", fake_search)
    patcher.setattr(core_fetch, "_request", lambda *a, **k: None)
    server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % server.server_port
    launch = {"headless": True}
    if NO_SANDBOX:
        launch["args"] = ["--no-sandbox"]
    if CHROME:
        launch["executable_path"] = CHROME
    rows = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch)
            for index in range(3):
                if index == 0:
                    shutil.rmtree(os.path.join(BASE, "cache"), ignore_errors=True)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.add_init_script(script="(" + INIT + ")();")
                page.goto(url + "/", wait_until="domcontentloaded")
                page.wait_for_selector("#nav button", timeout=15000)
                rail_ms = page.evaluate("() => performance.now() - window.__bench.start")
                page.wait_for_selector("#list .row:not(.skeleton)", timeout=15000)
                rows_ms = page.evaluate("() => performance.now() - window.__bench.start")
                page.wait_for_timeout(1500)
                cls = page.evaluate("() => window.__bench.cls || 0")
                nav = page.evaluate("() => { const n = performance.getEntriesByType('navigation')[0]; return n ? n.domContentLoadedEventEnd : null; }")
                shifts = page.evaluate("() => window.__bench.shifts || []")
                if shifts:
                    print("shifts", index, shifts[:5])
                rows.append({
                    "run": "cold" if index == 0 else "warm",
                    "rail_ms": round(rail_ms or 0, 1),
                    "rows_ms": round(rows_ms or 0, 1),
                    "dom_content_loaded_ms": round(nav or 0, 1),
                    "cls": round(cls or 0, 4),
                })
                context.close()
            browser.close()
    finally:
        patcher.undo()
        server.shutdown()
    print("| Run | Rail interactive ms | First row ms | DOMContentLoaded ms | CLS |")
    print("|---|---|---|---|---|")
    for row in rows:
        print("| %s | %.1f | %.1f | %.1f | %.4f |" % (
            row["run"], row["rail_ms"], row["rows_ms"],
            row["dom_content_loaded_ms"], row["cls"]))
    with open(os.path.join(os.getcwd(), "bench-ui.json"), "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2)


if __name__ == "__main__":
    main()
