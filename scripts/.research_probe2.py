"""Probe #2: corrected archive field encoding + extra candidate names.

Reuses mixcloud results already cached in /tmp/rave-probe.json.
"""
import json
import os
import time
import urllib.parse
import urllib.request

CACHE = "/tmp/rave-probe2.json"
OLD = "/tmp/rave-probe.json"
SLEEP = 0.55

EXTRA = [
    ("Telepathy", "promotion", "telepathy jungle"),
    ("Hysteria", "promotion", "hysteria rave"),
    ("Jungle Mania", "promotion", "jungle mania"),
    ("Warning", "promotion", "warning cambridge jungle"),
    ("Innovation", "promotion", "innovation drum and bass"),
    ("Club Kinetic", "promotion", "kinetic stoke"),
    ("Shelley's", "venue", "shelleys stoke"),
    ("The Institute", "venue", "institute birmingham"),
    ("Mr B's", "venue", "mr bs willenhall"),
    ("The Island", "venue", "island ilford"),
    ("Eclipse", "venue", "eclipse coventry"),
    ("The Haçienda", "venue", "hacienda"),
    ("Sunrise FM", "station", "sunrise fm"),
    ("LWR", "station", "lwr london"),
    ("Garage Nation", "promotion", "garage nation"),
    ("One In The Jungle", "promotion", "one in the jungle"),
]

OLD_NAMES = json.load(open(OLD))


def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rave-research/0.1"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.status in (429, 503):
                    print("RATE LIMITED", resp.status, file=__import__("sys").stderr)
                    return None
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print("ERR", attempt, exc, url[:90])
            time.sleep(2)
    return None


def archive(query, rows=3):
    url = ("https://archive.org/advancedsearch.php?q=" + urllib.parse.quote(query) +
           "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=date&rows=" + str(rows) + "&output=json")
    data = fetch(url)
    if not data:
        return None
    resp = data.get("response", {})
    return {"count": resp.get("numFound", 0),
            "titles": [(d.get("identifier"), d.get("title"), (d.get("date") or "")[:10])
                       for d in resp.get("docs", [])][:3]}


def mixcloud(query, limit=3):
    url = "https://api.mixcloud.com/search/?" + urllib.parse.urlencode(
        {"q": query, "type": "cloudcast", "limit": limit})
    data = fetch(url)
    if not data:
        return None
    items = [d.get("name") for d in data.get("data", [])][:3]
    return {"count": data.get("paging", {}).get("total") or len(items),
            "returned": len(items), "titles": items}


def main():
    plan = [(n, e["type"], e["mixcloud_query"]) for n, e in OLD_NAMES.items()] + EXTRA
    state = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    for i, (name, kind, mcq) in enumerate(plan, 1):
        if name in state and state[name].get("archive") and state[name]["archive"].get("titles"):
            continue
        entry = state.get(name) or {"type": kind, "mixcloud_query": mcq}
        quoted = '"%s"' % name
        scoped = 'collection:ravetapes AND %s' % quoted
        entry["archive_query"] = scoped
        entry["archive"] = archive(scoped)
        time.sleep(SLEEP)
        if entry["archive"] and entry["archive"]["count"] == 0:
            broad = '%s AND mediatype:audio' % quoted
            entry["archive_broad_query"] = broad
            entry["archive_broad"] = archive(broad)
            time.sleep(SLEEP)
        if name not in state or not state[name].get("mixcloud"):
            entry["mixcloud"] = mixcloud(mcq)
            time.sleep(SLEEP)
        else:
            entry["mixcloud"] = state[name]["mixcloud"]
        state[name] = entry
        print("%3d/%d %-20s arch=%s broad=%s mc=%s" % (
            i, len(plan), name,
            entry["archive"] and entry["archive"]["count"],
            entry.get("archive_broad") and entry["archive_broad"]["count"],
            entry["mixcloud"] and entry["mixcloud"]["count"]), flush=True)
        json.dump(state, open(CACHE, "w"), indent=1)


if __name__ == "__main__":
    main()
