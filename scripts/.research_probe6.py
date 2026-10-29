"""Probe #6: exact candidate catalogue queries -> /tmp/rave-final.json"""
import json
import time
import urllib.parse
import urllib.request

OUT = "/tmp/rave-final.json"
SLEEP = 0.5

# (name, type, archive_query or None, mixcloud_query or None)
PLAN = [
    ("Pure X", "promotion", 'collection:ravetapes AND title:("Pure X")', "pure x jungle"),
    ("Biology", "promotion", None, "biology rave"),
    ("Pandemonium", "promotion", 'title:("Pandemonium") AND mediatype:audio AND date:[1990-01-01 TO 2000-12-31]', "pandemonium rave"),
    ("Universe", "promotion", None, "universe rave"),
    ("Perception", "promotion", None, "perception rave"),
    ("Vision", "promotion", 'collection:ravetapes AND title:("Vision")', "vision rave 1993"),
    ("Hysteria", "promotion", 'collection:ravetapes AND title:("Hysteria")', "hysteria rave"),
    ("Warning", "promotion", 'title:("Warning") AND mediatype:audio AND ("One Nation" OR "Innovation" OR Junction OR Cambridge)', "warning cambridge jungle"),
    ("Innovation", "promotion", 'title:("Innovation") AND mediatype:audio AND (jungle OR rave OR hardcore OR "drum and bass")', "innovation drum and bass"),
    ("Club Kinetic", "promotion", 'title:("Club Kinetic") AND mediatype:audio', "kinetic stoke"),
    ("Jungle Mania", "promotion", 'title:("Jungle Mania") AND mediatype:audio', "jungle mania"),
    ("Tribal Gathering", "promotion", 'title:("Tribal Gathering") AND mediatype:audio', "tribal gathering"),
    ("Castlemorton", "promotion", 'title:("Castlemorton") AND mediatype:audio', "castlemorton"),
    ("Spiral Tribe", "promotion", 'title:("Spiral Tribe") AND mediatype:audio', "spiral tribe"),
    ("Sky Blue", "promotion", 'title:("Sky Blue") AND mediatype:audio AND (rave OR jungle OR Amnesia)', "sky blue amnesia"),
    ("Exodus", "promotion", None, "exodus collective"),
    ("Metalheadz", "promotion", 'title:("Metalheadz") AND mediatype:audio', "metalheadz"),
    ("Renegade Hardware", "promotion", 'title:("Renegade Hardware") AND mediatype:audio', "renegade hardware"),
    ("Elevation", "promotion", None, "elevation rave"),
    ("Desire", "promotion", None, "desire rave"),
    ("Kool London", "station", 'title:("Kool London") AND mediatype:audio', "kool london"),
    ("Rinse FM", "station", 'title:("Rinse FM") AND mediatype:audio', "rinse fm"),
    ("Rude FM", "station", 'title:("Rude FM") AND mediatype:audio', "rude fm"),
    ("Eruption FM", "station", 'title:("Eruption FM") AND mediatype:audio', "eruption fm"),
    ("Fantasy FM", "station", 'title:("Fantasy FM") AND mediatype:audio', "fantasy fm"),
    ("Weekend Rush", "station", 'title:("Weekend Rush") AND mediatype:audio', "weekend rush"),
    ("Conflict FM", "station", None, "conflict fm"),
    ("Demolition Cru", "station", None, "demolition cru"),
    ("Pulse FM", "station", None, "pulse fm 90.6"),
    ("Passion FM", "station", None, "passion fm"),
    ("Sunrise FM", "station", None, "sunrise fm"),
    ("The Sanctuary", "venue", 'title:("The Sanctuary") AND mediatype:audio AND (rave OR jungle OR hardcore)', "sanctuary rave"),
    ("Bagleys", "venue", 'title:("Bagleys") AND mediatype:audio', "bagleys"),
    ("Turnmills", "venue", 'title:("Turnmills") AND mediatype:audio', "turnmills"),
    ("The End", "venue", 'collection:ravetapes AND title:("The End")', "the end london club"),
    ("The Astoria", "venue", 'title:("Astoria") AND mediatype:audio AND (rave OR jungle OR "One Nation")', "astoria london rave"),
    ("Que Club", "venue", 'title:("Que Club") AND mediatype:audio', "que club"),
    ("Roller Express", "venue", 'title:("Roller Express") AND mediatype:audio', "roller express"),
    ("4 Aces", "venue", 'title:("4 Aces") AND (Ragga OR Labrynth OR jungle OR rave) AND mediatype:audio', "4 aces labrynth"),
    ("The Rocket", "venue", 'title:("The Rocket") AND mediatype:audio AND (rave OR jungle OR "One Nation")', "the rocket ilford"),
    ("Lazerdrome", "venue", None, "lazerdrome"),
    ("Pleasuredome", "venue", None, "pleasuredome 95"),
    ("Shelley's", "venue", None, "shelleys stoke"),
    ("Eclipse", "venue", None, "eclipse coventry"),
    ("The Institute", "venue", None, "institute birmingham"),
    ("Mr B's", "venue", None, "pandemonium mr bs"),
    ("The Island", "venue", None, "island ilford"),
    ("Blue Note", "venue", None, "metalheadz blue note"),
    ("Leisure Lounge", "venue", None, "metalheadz leisure lounge"),
    ("Club Colosseum", "venue", None, "club colosseum"),
    ("Club 414", "venue", None, "club 414 jungle"),
    ("Velvet Underground", "venue", 'title:("Velvet Underground") AND mediatype:audio AND (rave OR "Ultimate Base")', "velvet underground london"),
    ("Bowlers", "venue", 'collection:ravetapes AND title:("Bowlers")', "bowlers rave"),
    ("The Manor", "venue", 'title:("The Manor") AND mediatype:audio AND (rave OR jungle)', None),
    ("Labrynth", "venue", None, "labrynth rave"),
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


def archive(query, rows=4):
    url = ("https://archive.org/advancedsearch.php?q=" + urllib.parse.quote(query) +
           "&fl%5B%5D=identifier&fl%5B%5D=title&rows=" + str(rows) + "&output=json")
    data = fetch(url)
    if not data:
        return None
    resp = data.get("response", {})
    return {"count": resp.get("numFound", 0),
            "titles": [d.get("title") for d in resp.get("docs", [])][:4]}


def mixcloud(query, limit=3):
    url = "https://api.mixcloud.com/search/?" + urllib.parse.urlencode(
        {"q": query, "type": "cloudcast", "limit": limit})
    data = fetch(url)
    if not data:
        return None
    return {"returned": len(data.get("data", [])),
            "more": bool(data.get("paging", {}).get("next")),
            "titles": [d.get("name") for d in data.get("data", [])][:3]}


def main():
    out = []
    for name, kind, aq, mq in PLAN:
        item = {"name": name, "type": kind, "archive_query": aq, "mixcloud_query": mq}
        if aq:
            item["archive"] = archive(aq)
            time.sleep(SLEEP)
        if mq:
            item["mixcloud"] = mixcloud(mq)
            time.sleep(SLEEP)
        out.append(item)
        print("%-18s a=%s m=%s %s" % (name,
                                      (item.get("archive") or {}).get("count"),
                                      item.get("mixcloud") and item["mixcloud"]["returned"],
                                      (item.get("archive") or {}).get("titles", [None])[:1]), flush=True)
        json.dump(out, open(OUT, "w"), indent=1)


if __name__ == "__main__":
    main()
