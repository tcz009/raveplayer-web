"""Probe #3: title-scoped archive queries (precision) for every candidate name."""
import json
import os
import time
import urllib.parse
import urllib.request

CACHE = "/tmp/rave-probe3.json"
BASE = json.load(open("/tmp/rave-probe2.json"))
SLEEP = 0.55


def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rave-research/0.1"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.status in (429, 503):
                    print("RATE LIMITED", resp.status)
                    return None
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print("ERR", attempt, exc, url[:100])
            time.sleep(2)
    return None


def archive(query, rows=5):
    url = ("https://archive.org/advancedsearch.php?q=" + urllib.parse.quote(query) +
           "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=date&rows=" + str(rows) + "&output=json")
    data = fetch(url)
    if not data:
        return None
    resp = data.get("response", {})
    return {"count": resp.get("numFound", 0),
            "titles": [(d.get("title"), (d.get("date") or "")[:10])
                       for d in resp.get("docs", [])][:5]}


def main():
    state = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    names = list(BASE)
    for i, name in enumerate(names, 1):
        if name in state and state[name]:
            continue
        safe = name.replace('"', "")
        q = 'collection:ravetapes AND title:("%s")' % safe
        res = archive(q)
        state[name] = {"query": q, "result": res}
        print("%3d/%d %-20s %s %s" % (i, len(names), name,
                                      res and res["count"],
                                      res and [t[0] for t in res["titles"]][:2]), flush=True)
        json.dump(state, open(CACHE, "w"), indent=1)
        time.sleep(SLEEP)


if __name__ == "__main__":
    main()
