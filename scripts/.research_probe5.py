"""Probe #5: hand-refined archive queries (precision first)."""
import json
import os
import time
import urllib.parse
import urllib.request

CACHE = "/tmp/rave-probe5.json"
SLEEP = 0.5

QUERIES = [
    ("Pure X", 'collection:ravetapes AND title:("Pure X")'),
    ("Kool FM", 'collection:ravetapes AND title:("Kool FM")'),
    ("Kool London", 'title:("Kool London") AND mediatype:audio'),
    ("Don FM", 'title:("Don FM") AND mediatype:audio'),
    ("Centreforce", 'title:("Centreforce") AND mediatype:audio'),
    ("Rinse FM", 'title:("Rinse FM") AND mediatype:audio'),
    ("Pulse FM", 'title:("Pulse FM") AND mediatype:audio'),
    ("Passion FM", 'title:("Passion FM") AND mediatype:audio'),
    ("Weekend Rush", 'title:("Weekend Rush") AND mediatype:audio'),
    ("Eruption FM", 'title:("Eruption FM") AND mediatype:audio'),
    ("Rush FM", 'title:("Rush FM") AND mediatype:audio'),
    ("Thunder FM", 'title:("Thunder FM") AND mediatype:audio'),
    ("Rude FM", 'title:("Rude FM") AND mediatype:audio'),
    ("Flashback FM", 'title:("Flashback FM") AND mediatype:audio'),
    ("Radar FM", 'title:("Radar FM") AND mediatype:audio'),
    ("Upfront FM", 'title:("Upfront FM") AND mediatype:audio'),
    ("Conflict FM", 'title:("Conflict FM") AND mediatype:audio'),
    ("Demolition", 'title:("Demolition") AND mediatype:audio AND (jungle OR hardcore OR "Eruption")'),
    ("Fantasy FM", 'title:("Fantasy FM") AND mediatype:audio'),
    ("Sunrise FM", 'title:("Sunrise FM") AND mediatype:audio'),
    ("One In The Jungle", 'title:("One In The Jungle") AND mediatype:audio'),
    ("Hysteria", 'collection:ravetapes AND title:("Hysteria")'),
    ("Pandemonium", 'title:("Pandemonium") AND mediatype:audio AND date:[1990-01-01 TO 2000-12-31]'),
    ("Raindance", 'title:("Raindance") AND mediatype:audio AND (rave OR hardcore OR jungle)'),
    ("Universe", 'title:("Universe") AND mediatype:audio AND (rave OR hardcore OR jungle)'),
    ("Amnesia House", 'title:("Amnesia House") AND mediatype:audio'),
    ("Slammin' Vinyl", 'title:("Slammin") AND mediatype:audio'),
    ("Vibealite", 'title:("Vibealite") AND mediatype:audio'),
    ("Hardcore Heaven", 'title:("Hardcore Heaven") AND mediatype:audio'),
    ("Accelerated Culture", 'title:("Accelerated Culture") AND mediatype:audio'),
    ("Rezerection", 'title:("Rezerection") AND mediatype:audio'),
    ("Tribal Gathering", 'title:("Tribal Gathering") AND mediatype:audio'),
    ("Castlemorton", 'title:("Castlemorton") AND mediatype:audio'),
    ("Spiral Tribe", 'title:("Spiral Tribe") AND mediatype:audio'),
    ("Metalheadz", 'title:("Metalheadz") AND mediatype:audio'),
    ("Renegade Hardware", 'title:("Renegade Hardware") AND mediatype:audio'),
    ("Jungle Mania", 'title:("Jungle Mania") AND mediatype:audio'),
    ("Club Kinetic", 'title:("Club Kinetic") AND mediatype:audio'),
    ("Warning", 'title:("Warning") AND mediatype:audio AND (jungle OR rave OR hardcore)'),
    ("Innovation", 'title:("Innovation") AND mediatype:audio AND (jungle OR rave OR hardcore OR "drum and bass")'),
    ("The Sanctuary", 'title:("The Sanctuary") AND mediatype:audio AND (rave OR jungle OR hardcore)'),
    ("Bagleys", 'title:("Bagleys") AND mediatype:audio'),
    ("The Astoria", 'title:("Astoria") AND mediatype:audio AND (rave OR jungle OR "One Nation")'),
    ("Que Club", 'title:("Que Club") AND mediatype:audio'),
    ("Roller Express", 'title:("Roller Express") AND mediatype:audio'),
    ("4 Aces", 'title:("4 Aces") AND mediatype:audio'),
    ("Labrynth", 'title:("Labrynth") AND mediatype:audio'),
    ("The Rocket", 'title:("The Rocket") AND mediatype:audio AND (rave OR jungle OR "One Nation" OR hardcore)'),
    ("Lazerdrome", 'title:("Lazerdrome") AND mediatype:audio'),
    ("Pleasuredome", 'title:("Pleasuredome") AND mediatype:audio'),
    ("The Manor", 'title:("The Manor") AND mediatype:audio AND (rave OR jungle)'),
    ("Velvet Underground", 'title:("Velvet Underground") AND mediatype:audio AND (rave OR "Ultimate Base")'),
    ("Turnmills", 'title:("Turnmills") AND mediatype:audio'),
    ("The End", 'title:("The End") AND mediatype:audio AND (rave OR jungle OR "drum and bass")'),
    ("Desire", 'title:("Desire") AND mediatype:audio AND (rave OR jungle)'),
    ("Orange", 'title:("Orange") AND mediatype:audio AND (rave OR jungle OR hardcore)'),
    ("Vision", 'collection:ravetapes AND title:("Vision")'),
    ("Sterns", 'collection:ravetapes AND title:("Sterns")'),
    ("Kaos", 'collection:ravetapes AND title:("Kaos")'),
    ("Telepathy", 'collection:ravetapes AND title:("Telepathy")'),
    ("Garage Nation", 'title:("Garage Nation") AND mediatype:audio'),
    ("Hacienda", 'title:("Hacienda") AND mediatype:audio'),
    ("Club Labyrinth", 'title:("Labyrinth") AND mediatype:audio AND (rave OR jungle OR Dreamscape)'),
    ("Shelley\'s", 'title:("Shelleys") AND mediatype:audio'),
    ("Eclipse", 'title:("Eclipse") AND mediatype:audio AND (rave OR jungle OR hardcore)'),
    ("Biology", 'title:("Biology") AND mediatype:audio AND rave'),
]


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
            "titles": [d.get("title") for d in resp.get("docs", [])][:5]}


def main():
    state = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    for i, (name, q) in enumerate(QUERIES, 1):
        if name in state and state[name]:
            continue
        res = archive(q)
        state[name] = {"query": q, "result": res}
        print("%2d %-20s %5s %s" % (i, name, res and res["count"],
                                    (res and res["titles"][:2])), flush=True)
        json.dump(state, open(CACHE, "w"), indent=1)
        time.sleep(SLEEP)


if __name__ == "__main__":
    main()
