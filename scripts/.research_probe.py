"""One-off probe: archive.org + mixcloud coverage for UK rave names.

Writes /tmp/rave-probe.json so the analysis can be re-read without re-hitting
the APIs. Not part of the app; delete after use.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

CACHE = "/tmp/rave-probe.json"
SLEEP = 0.55  # ~1.8 req/s

NAMES = [
    # promotions / club nights
    ("Pure X", "promotion", "pure x rave"),
    ("Biology", "promotion", "biology rave"),
    ("Pandemonium", "promotion", "pandemonium rave"),
    ("Raindance", "promotion", "raindance rave"),
    ("Universe", "promotion", "universe rave"),
    ("Living Dream", "promotion", "living dream rave"),
    ("Vision", "promotion", "vision rave"),
    ("Perception", "promotion", "perception rave"),
    ("AWOL", "promotion", "awol jungle"),
    ("Jungle Fever", "promotion", "jungle fever"),
    ("Desire", "promotion", "desire rave"),
    ("Roast", "promotion", "roast rave"),
    ("World Dance", "promotion", "world dance"),
    ("One Nation", "promotion", "one nation"),
    ("Fantazia", "promotion", "fantazia"),
    ("Helter Skelter", "promotion", "helter skelter"),
    ("Dreamscape", "promotion", "dreamscape"),
    ("Amnesia House", "promotion", "amnesia house"),
    ("Rezerection", "promotion", "rezerection"),
    ("Slammin' Vinyl", "promotion", "slammin vinyl"),
    ("Vibealite", "promotion", "vibealite"),
    ("Hardcore Heaven", "promotion", "hardcore heaven"),
    ("Accelerated Culture", "promotion", "accelerated culture"),
    ("Seduction", "promotion", "seduction rave"),
    ("Elevation", "promotion", "elevation rave"),
    ("Sun City", "promotion", "sun city rave"),
    ("Club Labyrinth", "promotion", "labyrinth rave"),
    ("The Big Love", "promotion", "big love rave"),
    ("Tribal Gathering", "promotion", "tribal gathering"),
    ("Castlemorton", "promotion", "castlemorton"),
    ("Spiral Tribe", "promotion", "spiral tribe"),
    ("Exodus", "promotion", "exodus rave"),
    ("DiY", "promotion", "diy rave"),
    ("Bedlam", "promotion", "bedlam rave"),
    ("Orange", "promotion", "orange rave"),
    ("Feel Real", "promotion", "feel real rave"),
    ("Freedom", "promotion", "freedom rave"),
    ("Energy", "promotion", "energy rave"),
    ("Sterns", "promotion", "sterns rave"),
    ("Hardcore Takeover", "promotion", "hardcore takeover"),
    ("Kaos", "promotion", "kaos rave"),
    ("Sky Blue", "promotion", "sky blue rave"),
    ("Metalheadz", "promotion", "metalheadz"),
    ("Speed", "promotion", "fabio grooverider speed"),
    ("Movement", "promotion", "movement drum and bass"),
    ("Swerve", "promotion", "swerve drum and bass"),
    ("Hardware", "promotion", "hardware drum and bass"),
    ("Funktion", "promotion", "funktion drum and bass"),
    ("Bad Company", "promotion", "bad company drum and bass"),
    # venues
    ("The Sanctuary", "venue", "sanctuary rave"),
    ("Bagleys", "venue", "bagleys"),
    ("Club UK", "venue", "club uk"),
    ("Turnmills", "venue", "turnmills"),
    ("Ministry of Sound", "venue", "ministry of sound"),
    ("The End", "venue", "the end london club"),
    ("Fabric", "venue", "fabric london"),
    ("The Astoria", "venue", "astoria london rave"),
    ("The Forum", "venue", "forum london rave"),
    ("Bowlers", "venue", "bowlers rave"),
    ("Pleasuredome", "venue", "pleasuredome"),
    ("The Rocket", "venue", "rocket london rave"),
    ("Roller Express", "venue", "roller express"),
    ("Lazerdrome", "venue", "lazerdrome"),
    ("4 Aces", "venue", "4 aces rave"),
    ("The Manor", "venue", "the manor rave"),
    ("The Que Club", "venue", "que club"),
    ("Mr C's", "venue", "mr cs"),
    ("The Cross", "venue", "the cross london club"),
    ("Velvet Underground", "venue", "velvet underground london"),
    ("Leisure Lounge", "venue", "leisure lounge"),
    ("Club 414", "venue", "club 414"),
    ("Club Colosseum", "venue", "colosseum london"),
    ("Blue Note", "venue", "blue note london"),
    # pirate stations
    ("Kool FM", "station", "kool fm"),
    ("Don FM", "station", "don fm"),
    ("Centreforce", "station", "centreforce"),
    ("Rinse FM", "station", "rinse fm"),
    ("Pulse", "station", "pulse fm"),
    ("Passion", "station", "passion fm"),
    ("Weekend Rush", "station", "weekend rush"),
    ("Eruption", "station", "eruption fm"),
    ("Rush", "station", "rush fm"),
    ("Thunder", "station", "thunder fm"),
    ("Rude FM", "station", "rude fm"),
    ("Flashback", "station", "flashback fm"),
    ("Radar", "station", "radar fm"),
    ("Upfront", "station", "upfront fm"),
    ("Conflict", "station", "conflict fm"),
    ("Demolition", "station", "demolition fm"),
    ("Fantasy FM", "station", "fantasy fm"),
    ("Kool London", "station", "kool london"),
]


def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rave-research/0.1"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.status in (429, 503):
                    print("RATE LIMITED", resp.status, url, file=sys.stderr)
                    return None
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print("ERR", attempt, exc, url[:90], file=sys.stderr)
            time.sleep(2)
    return None


def archive(query, rows=3):
    url = ("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode({
        "q": query, "fl[]": ["identifier", "title"], "rows": rows, "output": "json",
    }))
    data = fetch(url)
    if not data:
        return None
    resp = data.get("response", {})
    return {"count": resp.get("numFound", 0),
            "titles": [d.get("title") for d in resp.get("docs", [])][:3]}


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
    state = {}
    if os.path.exists(CACHE):
        state = json.load(open(CACHE))
    for i, (name, kind, mcq) in enumerate(NAMES, 1):
        if name in state and state[name].get("mixcloud"):
            continue
        entry = state.get(name) or {"type": kind, "mixcloud_query": mcq}
        quoted = '"%s"' % name
        scoped = 'collection:ravetapes AND %s' % quoted
        entry["archive_query"] = scoped
        entry["archive"] = archive(scoped)
        time.sleep(SLEEP)
        entry["mixcloud"] = mixcloud(mcq)
        time.sleep(SLEEP)
        if entry["archive"] and entry["archive"]["count"] == 0:
            broad = '%s AND mediatype:audio' % quoted
            entry["archive_broad_query"] = broad
            entry["archive_broad"] = archive(broad)
            time.sleep(SLEEP)
        state[name] = entry
        print("%3d/%d %-22s arch=%s mc=%s" % (
            i, len(NAMES), name,
            entry["archive"] and entry["archive"]["count"],
            entry["mixcloud"] and entry["mixcloud"]["count"]), flush=True)
        json.dump(state, open(CACHE, "w"), indent=1)


if __name__ == "__main__":
    main()
