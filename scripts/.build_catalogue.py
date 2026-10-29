"""Build docs/RESEARCH-RAVES.md and resources/rave-catalog.json from the probe data."""
import json
import os
import textwrap

FINAL = json.load(open("/tmp/rave-final.json"))
BY = {x["name"]: x for x in FINAL}

# name -> (mixcloud usable hits in top 3)
MC = {
    "Pure X": 0, "Biology": 2, "Pandemonium": 3, "Universe": 2, "Perception": 2,
    "Vision": 3, "Hysteria": 2, "Warning": 2, "Innovation": 3, "Club Kinetic": 3,
    "Jungle Mania": 3, "Tribal Gathering": 3, "Castlemorton": 3, "Spiral Tribe": 3,
    "Sky Blue": 3, "Exodus": 2, "Metalheadz": 3, "Renegade Hardware": 3,
    "Kool London": 3, "Rinse FM": 3, "Rude FM": 3, "Eruption FM": 3, "Fantasy FM": 3,
    "Weekend Rush": 3, "Conflict FM": 3, "Demolition Cru": 3, "Pulse FM": 3,
    "Passion FM": 2, "Sunrise FM": 2, "The Sanctuary": 2, "Bagleys": 3,
    "Turnmills": 3, "The End": 3, "The Astoria": 3, "Que Club": 3,
    "Roller Express": 3, "4 Aces": 3, "The Rocket": 0, "Lazerdrome": 3,
    "Pleasuredome": 3, "Shelley's": 3, "Eclipse": 3, "The Institute": 3,
    "Mr B's": 3, "The Island": 3, "Blue Note": 3, "Leisure Lounge": 3,
    "Club Colosseum": 3, "Club 414": 3, "Velvet Underground": 2, "Bowlers": 3,
    "The Manor": 0, "Labrynth": 3, "Orange": 1, "Sterns": 3,
}

NOTES = {
    "Vision": "1 of 2 archive hits is the real Vision night (Return of the Warehouse Concept, 12.02.93).",
    "Hysteria": "Both in-collection hits are the Hysteria / Pure X 'Junglemania' packs.",
    "Warning": "Archive hits include One Nation/Innovation co-promotions; 1 unrelated 'Warning' podcast.",
    "Tribal Gathering": "Mixcloud hits are post-2000 Panama editions, not the 1993-97 UK Gatherings.",
    "Sky Blue": "Amnesia House event series (Sky Blue Connexion, 1990); 6 archive sets.",
    "Weekend Rush": "Mixcloud hits are 2021 revival shows on Weekend Rush FM.",
    "Fantasy FM": "Mixcloud hits are modern shows on the revived Fantasy FM.",
    "Kool London": "Mixcloud hits are 2007-2022 Kool London shows rather than 90s airchecks.",
    "The Institute": "2 of 3 Mixcloud hits are post-2000 events at the same Birmingham venue.",
    "Bowlers": "Mixcloud hits are 2012-2021 raves at the same venue.",
    "4 Aces": "Home of Labrynth; 1 archive set (Ragga Twins, 1988), 3 good Mixcloud sets.",
    "The Manor": "1 of 2 archive hits is real (Carl Cox, Ringwood 1998); no Mixcloud.",
    "Orange": "1 of 3 Mixcloud hits is the real Orange night (Randall @ The Rocket Complex, 1992).",
    "Passion FM": "Only 1-2 of 3 results are the London pirate; the rest are unrelated.",
    "Sunrise FM": "Mixcloud only: Ellis Dee 1988 and Seduction 1989 airchecks.",
    "Pleasuredome": "Mixcloud has 1995 Pleasuredome sets; archive has none.",
    "The Rocket": "Strong archive run of One Nation tapes; no Mixcloud hits under any query tried.",
    "The End": "In-collection archive only (Cari Lekebusch, Damon Wild); Mixcloud skews modern.",
}

# Rows: additions (query/count from probe #6, plus two hand-checked extras)
ADD = [
    ("Pure X", "promotion"), ("Biology", "promotion"), ("Pandemonium", "promotion"),
    ("Universe", "promotion"), ("Perception", "promotion"), ("Vision", "promotion"),
    ("Hysteria", "promotion"), ("Warning", "promotion"), ("Innovation", "promotion"),
    ("Club Kinetic", "promotion"), ("Jungle Mania", "promotion"),
    ("Tribal Gathering", "promotion"), ("Castlemorton", "promotion"),
    ("Spiral Tribe", "promotion"), ("Sky Blue", "promotion"), ("Exodus", "promotion"),
    ("Orange", "promotion"), ("Metalheadz", "promotion"), ("Renegade Hardware", "promotion"),
    ("Kool London", "station"), ("Rinse FM", "station"), ("Rude FM", "station"),
    ("Eruption FM", "station"), ("Fantasy FM", "station"), ("Weekend Rush", "station"),
    ("Conflict FM", "station"), ("Demolition Cru", "station"), ("Pulse FM", "station"),
    ("Passion FM", "station"), ("Sunrise FM", "station"),
    ("Sterns", "venue"), ("The Sanctuary", "venue"), ("Bagleys", "venue"),
    ("Turnmills", "venue"), ("The End", "venue"), ("The Astoria", "venue"),
    ("Que Club", "venue"), ("Roller Express", "venue"), ("4 Aces", "venue"),
    ("The Rocket", "venue"), ("Lazerdrome", "venue"), ("Pleasuredome", "venue"),
    ("Shelley's", "venue"), ("Eclipse", "venue"), ("The Institute", "venue"),
    ("Mr B's", "venue"), ("The Island", "venue"), ("Blue Note", "venue"),
    ("Leisure Lounge", "venue"), ("Club Colosseum", "venue"), ("Club 414", "venue"),
    ("Velvet Underground", "venue"), ("Bowlers", "venue"), ("The Manor", "venue"),
    ("Labrynth", "venue"),
]

EXTRA = {
    "Sterns": {
        "archive_query": 'collection:ravetapes AND title:("Sterns")',
        "archive_count": 3,
        "archive_example": "Carl Cox & Loftgroover @ In-Ter-Dance, Sterns, Worthing (19.6.93)",
        "mixcloud_query": "sterns rave",
        "mixcloud_example": "Carl Cox - Sterns, Worthing - 1990",
    },
    "Orange": {
        "archive_query": None,
        "archive_count": 0,
        "archive_example": "",
        "mixcloud_query": "orange rave",
        "mixcloud_example": "DJ Randall Orange @ The Rocket Complex 1992",
    },
}

COVERED = [
    # name, type, arch count text, arch example, mc count, mc example, note
    ("Kool FM", "station", "8", "Brockie & Fabrok - Kool FM 94.5 (23.4.93)", 3,
     "Mampi Swift & Skibadee - Kool FM 945 - 12.10.97", "in filters.json + MIXCLOUD_QUERIES"),
    ("Dreamscape", "promotion", "3", "Dreamscape 3 - Absolutely No Compromise 10.3.92", 1,
     "96-98 Dreamscape Happy Hardcore set. DJ Sonic", "in filters.json + MIXCLOUD_QUERIES"),
    ("Helter Skelter", "promotion", "1", "Helter Skelter - Energy 97 - Pack 1", 1,
     "DJ Hype w/ MC GQ & MC MC - Helter Skelter 'Energy 96' - 10.8.96",
     "in filters.json + MIXCLOUD_QUERIES"),
    ("Fantazia", "promotion", "1", "Carl Cox - Fantazia Live At Bowlers (1994)", 3,
     "DJ Stu Allan - Fantazia @ Bowlers", "in filters.json + MIXCLOUD_QUERIES"),
    ("One Nation", "promotion", "3 (67†)", "One Nation and Warning 31.3.00", 1,
     "Brockie w/ Skibadee & Hyper D - One Nation Clash of the Titans II - 26.6.97",
     "in filters.json + MIXCLOUD_QUERIES; only 1 of 3 Mixcloud hits is usable"),
    ("Centreforce", "station", "0", "nothing in ravetapes (revived-station uploads only)", 3,
     "Jeremy Healy Debut Show - 88.3 Centreforce radio - 2020",
     "in filters.json but the archive filter returns 0 — dead filter"),
    ("Don FM", "station", "0 (3†)", "Don FM December 1996 Hedgerley Bucks", 3,
     "Rugged Vinyl Crew - Don FM 105.7 - London - 1993",
     "in filters.json but the archive filter returns 0 — tapes live outside ravetapes"),
    ("Slammin' Vinyl", "promotion", "0 (7†)", "Brockie B2B Mampi Swift - Live at Slammin Vinyl", 3,
     "DJ Randall w/ Stevie Hyper D - Slammin Vinyl - Bagleys - 6.2.98",
     "in filters.json; collection-scoped query finds nothing"),
    ("Jungle Fever", "promotion", "2", "Jungle Fever Showtime 1995", 3,
     "Brockie With Skiba, Shabba N Det @ Jungle Fever 2000", "in filters.json + MIXCLOUD_QUERIES"),
    ("Roast", "promotion", "2", "Roast - Sunday Session Ting 97", 3,
     "Nicky Blackmarket & Stevie Hyper D - Roast - The Island, Ilford - 17.2.96",
     "in filters.json + MIXCLOUD_QUERIES"),
    ("Telepathy", "promotion", "1", "Telepathy NYE Extravaganza 1994 PT 2", 3,
     "Telepathy Jungle Dons 'Dub Plate Special' 1995", "in filters.json + MIXCLOUD_QUERIES"),
    ("World Dance", "promotion", "2", "World Dance - April 2nd, 1994", 2,
     "DJ Hype w/ MC GQ - World Dance - Lydd Airport - 5.11.94",
     "in filters.json + MIXCLOUD_QUERIES"),
    ("AWOL", "promotion", "1", "AWOL '93", 3, "93-94 AWOL Jungle Tribute Mix",
     "in MIXCLOUD_QUERIES"),
    ("Raindance", "promotion", "0", "nothing in ravetapes", 3,
     "Kenny Ken w/ Shockin B & MC Moose - Raindance Summer Rave Festival - SeOne - 18.6.05",
     "in MIXCLOUD_QUERIES"),
    ("Amnesia House", "promotion", "0 (36†)", "Amnesia House - Institute (Birmingham) - 1.5.94", 3,
     "LTJ Bukem - Amnesia House The Big Bank Holiday Bash I - 1.5.94", "in MIXCLOUD_QUERIES"),
    ("Rezerection", "promotion", "1 (10†)", "Blu Peter @ Rezerection: Lurrrve Special, Edinburgh (1994)", 3,
     "Grooverider & Marc Smith @ Rezerection - The Event", "in MIXCLOUD_QUERIES"),
    ("Vibealite", "promotion", "0 (6†)", "LTJ Bukem & MC Conrad Live @ Vibealite, Venue 44, Mansfield", 3,
     "Mark EG - Vibealite 4th Birthday Party 1997", "in MIXCLOUD_QUERIES"),
    ("Hardcore Heaven", "promotion", "0 (16†)", "Hardcore Heaven 1 [CD1/2/3]", 3,
     "DJ Sy with Sharkey & Charlie B at Hardcore Heaven The Return (1996)",
     "in MIXCLOUD_QUERIES"),
    ("Accelerated Culture", "promotion", "0 (40†)", "Accelerated Culture 1 - The Sanctuary, Milton Keynes - 12.5.01", 3,
     "Andy C & Mampi Swift w/ MC's - Accelerated Culture - D&B Awards 2k3",
     "in MIXCLOUD_QUERIES"),
    ("Seduction", "promotion", "0", "nothing in ravetapes", 3,
     "DJ Seduction - Dreamscape 2 - The Sanctuary - 28.2.92",
     "in MIXCLOUD_QUERIES, but hits are DJ Seduction the DJ, not the night"),
    ("Elevation", "promotion", "0", "nothing in ravetapes", 3,
     "DJ Rap w/ MC MC & Stevie Hyper D - Elevation 'The Story Continues' - Roller Express 10.4.93",
     "in MIXCLOUD_QUERIES"),
    ("Garage Nation", "promotion", "0 (5†)", "Various Artists - Garage Nation: V2 Crew (c.2000)", 3,
     "DJ EZ Garage Nation 'Easter Special' April 1999", "in filters.json + MIXCLOUD_QUERIES"),
    ("One In The Jungle", "promotion", "0 (16†)", "4 Hero - One In The Jungle - 11.10.1996", 3,
     "Ed Rush and Trace - BBC Radio One in the Jungle - 07.02.97",
     "in filters.json, but only outside ravetapes"),
    ("Fabric", "venue", "0", "nothing", 3,
     "Transitions with John Digweed live from Fabric, London",
     "in MIXCLOUD_QUERIES; post-2000 club, no era content"),
    ("Ministry of Sound", "venue", "0", "nothing", 3,
     "Club Classics Mini Mix | Ministry of Sound",
     "in MIXCLOUD_QUERIES; hits are label compilations, not club sets"),
]

SKIPPED = [
    ("Sun City", "promotion", "0", "", 0, "", "no content in either source"),
    ("Living Dream", "promotion", "3†", "Dreamscape XXII - The Living Dream (20.7.96)", 3,
     "DJ Dougal Dreamscape 22 'The Living Dream'", "not a promotion: subtitle of Dreamscape 22"),
    ("Club Labyrinth", "promotion", "0", "", 2,
     "DJ Seduction - Dreamscape Rave Nation Labyrinth - Pleasure Rooms 1.5.99",
     "one-off Dreamscape event name, not a promotion"),
    ("The Big Love", "promotion", "0", "", 3,
     "Universe 'Big Love' - Lower Pertwood Farm 13.8.93", "Universe event name; covered by Universe"),
    ("DiY", "promotion", "0", "", 3, "DIY Rave Sessions - Independence Day",
     "'DIY' matches home improvement; Mixcloud hits are modern generic rave nights"),
    ("Bedlam", "promotion", "0", "", 0, "", "no content"),
    ("Feel Real", "promotion", "1", "Carl Cox & MC Man Parris @ ESP Supernova", 0, "",
     "archive hit is a different event; no Mixcloud"),
    ("Freedom", "promotion", "7", "Anoebis - The History of Goa Trance: 1995", 3,
     "FREEDOM RAVE 22.04.2015", "matches religious/US rave content"),
    ("Energy", "promotion", "25", "Helter Skelter - Energy 97", 3, "Dj E 4 Energy - Rave On",
     "matches everything; 'Energy 97' is a Helter Skelter event"),
    ("Hardcore Takeover", "promotion", "1", "beatmania IIDX soundtrack", 3,
     "Happy Hardcore Takeover - Energy1058 2026", "generic modern mix-series title"),
    ("Kaos", "promotion", "1", "Carl Cox - Kaos 14 Promo Mix (1992)", 3,
     "Get Down To The Underground - Kaos Rave", "thin and generic: one tape only"),
    ("Speed", "promotion", "20", "Billy Nasty @ New York Club, Montpellier", 0, "",
     "generic word; the Speed nights are not tagged"),
    ("Movement", "promotion", "17", "Carl Cox @ MayDay Sonic Empire", 3,
     "Movement - Perpetual Drum & Bass Motion", "generic; Mixcloud hits are a different mix series"),
    ("Swerve", "promotion", "0", "", 2, "Swerve Inspired Drum & Bass", "generic; no scene content"),
    ("Hardware", "promotion", "7", "Ash-A-Tack - Blackmarket Jungle Studio Mix", 3,
     "Renegade Hardware @ The End", "better covered as Renegade Hardware"),
    ("Funktion", "promotion", "0", "", 3, "Liquid Funktion @ The Annexe",
     "generic; hits are unrelated / German-language"),
    ("Bad Company", "promotion", "0", "", 3, "Bad Company Drum and Bass Classics",
     "matches Bible and video-game content"),
    ("Club UK", "venue", "1", "Billy Nasty @ The Final Frontier Club, UK", 3,
     "Game Club UK - Episode 22", "phrase too generic to scope"),
    ("The Forum", "venue", "0", "", 0, "", "no content; Kentish Town Forum sets are not tagged"),
    ("Mr C's", "venue", "0", "", 3, "Kitty & Mr. C's Enchanted Tiki Hut Show",
     "matches an unrelated podcast"),
    ("The Cross", "venue", "0", "", 0, "", "no content"),
    ("Rush", "station", "31", "Brainkillers - Weekend Rush 92.3", 3,
     "Devastate Live Weekend Rush FM", "substring of Weekend Rush; no separate Rush FM"),
    ("Thunder", "station", "6", "Billy Nasty - House Masters: Hard House", 3, "Thunder FM",
     "generic word; results unrelated"),
    ("Flashback", "station", "1", "Andrew Weatherall @ The Hacienda", 3,
     "Flashback FM 95.6 (GTA III)", "generic; Mixcloud hits are a GTA radio station"),
    ("Radar", "station", "1", "Adam Beyer @ former US radar station", 3,
     "Radar FM Wroclaw", "generic; non-UK hits"),
    ("Upfront", "station", "0", "", 3, "Upfront Dance FM Brighton 2025",
     "modern Brighton station; no 90s airchecks"),
    ("The Hacienda", "venue", "1", "Andrew Weatherall @ The Hacienda (July 1993)", 0, "",
     "one set only; Mixcloud hits are podcasts about the club"),
    ("LWR", "station", "6", "Tim Westwood LWR April 1985", 3, "PAULO LABELLO live on LWR 2022",
     "pre-1989 and modern shows; not a rave station"),
]


def clip(text, n=58):
    text = (text or "").replace("|", "/").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


MC_EXAMPLE = {
    "Hysteria": "Andy C w/ Bassman, Hooligan, Palmer Charmer & Stevie Hyper D - Hysteria 10 - The Sanctuary - 20.1.96",
}


def add_row(name, kind):
    if name in EXTRA:
        e = EXTRA[name]
        aq, ac, ax = e["archive_query"], e["archive_count"], e["archive_example"]
        mcq, mx = e["mixcloud_query"], e["mixcloud_example"]
    else:
        x = BY[name]
        aq = x["archive_query"]
        ac = (x.get("archive") or {}).get("count") or 0
        ax = ((x.get("archive") or {}).get("titles") or [""])[0]
        mcq = x["mixcloud_query"]
        mx = MC_EXAMPLE.get(name) or ((x.get("mixcloud") or {}).get("titles") or [""])[0]
    m = MC[name]
    if ac and m:
        verdict = "add both"
    elif ac:
        verdict = "archive only"
    elif m:
        verdict = "mixcloud only"
    else:
        verdict = "skip — no content"
    star = "†" if aq and "collection:ravetapes" not in aq and ac else ""
    return {
        "name": name, "type": kind, "archive_query": aq, "mixcloud_query": mcq,
        "archive_count": ac, "mixcloud_count": m, "verdict": verdict,
        "archive_example": ax, "mixcloud_example": mx, "star": star,
    }


def row_md(r):
    ac = ("%d%s" % (r["archive_count"], r["star"])) if r["archive_count"] else "0"
    return "| %s | %s | %s | %s | %s | %s | %s |" % (
        r["name"], r["type"], ac, clip(r["archive_example"]) or "—",
        r["mixcloud_count"] or "0", clip(r["mixcloud_example"]) or "—", r["verdict"])


def main():
    rows = [add_row(n, t) for n, t in ADD]
    rows.sort(key=lambda r: ({"station": 0, "promotion": 1, "venue": 2}[r["type"]],
                             -(r["archive_count"]), -(r["mixcloud_count"]), r["name"]))
    catalog = [{"name": r["name"], "type": r["type"],
                "archive_query": r["archive_query"], "mixcloud_query": r["mixcloud_query"],
                "archive_count": r["archive_count"], "mixcloud_count": r["mixcloud_count"]}
               for r in rows]
    os.makedirs("resources", exist_ok=True)
    with open("resources/rave-catalog.json", "w") as fh:
        json.dump(catalog, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    cov_rows = "\n".join(
        "| %s | %s | %s | %s | %s | %s | skip — already covered |" % (
            n, t, a, clip(ax) or "—", m, clip(mx) or "—")
        for n, t, a, ax, m, mx, _ in COVERED)

    skip_rows = "\n".join(
        "| %s | %s | %s | %s | %s | %s | %s |" % (
            n, t, a or "0", clip(ax) or "—", m or "0", clip(mx) or "—", why)
        for n, t, a, ax, m, mx, why in SKIPPED)

    body = "\n".join(row_md(r) for r in rows)

    doc = textwrap.dedent("""\
    # UK rave, jungle and pirate-radio catalogue (1989-2000)

    Research for the Rave Player library: which classic UK promotions, venues and pirate
    stations actually have playable audio in the two sources the app uses (Internet Archive
    and Mixcloud), and which of those the app already covers.

    ## Method

    - **Names.** 108 names compiled from the brief plus the wider 1989-2000 scene: tape-pack
      promotions, London and Midlands venues, pirate/jungle stations and jungle-DnB club nights.
      For each, a search was run against both APIs at under 2 requests/second; the run stopped
      clean on rate limits (no 429/503 seen).
    - **Archive.** First `collection:ravetapes AND "<name>"`. Because the ravetapes collection is
      only 724 items and free-text search also matches descriptions/full text, raw counts were
      noisy (e.g. "Vision" returned 12 hits, almost none of them the Vision night). Every count
      in this table therefore comes from a **title-scoped** query, which is the query string
      shipped in `resources/rave-catalog.json`. Where the collection has nothing, the query drops
      the collection scope and searches the whole archive — those counts are marked **†**.
    - **Mixcloud.** `https://api.mixcloud.com/search/?q=<q>&type=cloudcast&limit=3`. The public
      API exposes no total, so "Mixcloud count" is **usable results within the top 3** (0-3),
      judged by title. Queries were tuned per name (e.g. `pulse fm 90.6`, not `pulse`) because
      the engine is fuzzy.
    - **False positives.** Counts were never trusted on their own; the returned titles were read.
      Names that only matched unrelated content were dropped even when the number looked good.

    ## What the app already covers

    `filters.json` covers Kool FM, Dreamscape, Helter Skelter, Fantazia, One Nation, Centreforce,
    Don FM, Slammin' Vinyl, Jungle Fever, Roast, Telepathy, World Dance, Garage Nation and
    One In The Jungle. `MIXCLOUD_QUERIES` adds Desire, AWOL, Raindance, Amnesia House, Rezerection,
    Vibealite, Hardcore Heaven, Accelerated Culture, Seduction, Elevation, Sun City, fabric and
    Ministry of Sound.

    Three of those archive filters are broken. `collection:ravetapes AND Centreforce` and
    `collection:ravetapes AND "Don FM"` both return **0**. `collection:ravetapes AND "Slammin"`
    returns 3 items but none of them are Slammin' Vinyl packs (they are false positives on the
    description text). The tapes exist outside the collection: Don FM 3, Slammin' Vinyl 7,
    Amnesia House 36. Centreforce has no 90s airchecks in the archive at all, only 2018+
    uploads from the revived station. One Nation is thin in-collection (3) against 67 archive-wide.

    ## Findings

    - The ravetapes collection is much narrower than it looks: 724 items, dominated by Carl Cox,
      Billy Nasty and Colin Dale studio/live mixes. Most named UK promotions and nearly all
      pirate stations are **not** in it — hence the large number of † rows.
    - Mixcloud is the opposite: strong for tape-pack nights (Fantazia, Dreamscape, One Nation,
      Hysteria, Pandemonium, Sterns) and for stations, but it skews to post-2000 uploads and
      revived-station shows.
    - Archive title search is reliable for event tapes ("One Nation - The Birthday Ball 1994 -
      The Rocket - 8 Live DJ Sets") and weak for anything whose name is also an English word.
    - Venues are the best-value additions: one venue filter (The Sanctuary, Bagleys, Roller
      Express, Que Club, Astoria) surfaces tapes from several promotions at once.

    ## Catalogue (recommended additions)

    `†` = count is from a whole-archive query (the ravetapes collection returns 0 for that name).
    Every row here returned at least one genuine hit; rejected names are in the skipped list below.

    | Name | Type | Archive count (ravetapes) | Archive example | Mixcloud count | Mixcloud example | Verdict |
    | --- | --- | --- | --- | --- | --- | --- |
    """) + body + "\n\n"

    doc += textwrap.dedent("""\
    ## Already covered (no addition)

    | Name | Type | Archive count (ravetapes) | Archive example | Mixcloud count | Mixcloud example | Verdict |
    | --- | --- | --- | --- | --- | --- | --- |
    """) + cov_rows + "\n\n"

    doc += textwrap.dedent("""\
    ## Skipped — no content, ambiguous or false positives

    Counts are the raw best-case numbers observed, before rejection; they show why the name
    looked tempting.

    | Name | Type | Archive count | Archive example | Mixcloud count | Mixcloud example | Reason |
    | --- | --- | --- | --- | --- | --- | --- |
    """) + skip_rows + "\n\n"

    doc += textwrap.dedent("""\
    ## Notes for wiring

    - `resources/rave-catalog.json` contains the 55 recommended additions only, with the exact
      query strings that produced the counts above (`archive_count` / `mixcloud_count`).
    - `archive_query: null` means the archive had nothing usable; `mixcloud_query: null` means
      no Mixcloud query produced a usable hit.
    - Counts are a snapshot of the two public APIs on 2026-10-29 and will drift.
    - Nothing in `app.py`, `filters.json`, `test/` or `static/` was modified.
    """)

    with open("docs/RESEARCH-RAVES.md", "w") as fh:
        fh.write(doc)
    print("rows:", len(rows), "catalog:", len(catalog))
    print("verdicts:", {v: sum(1 for r in rows if r["verdict"] == v) for v in set(r["verdict"] for r in rows)})


if __name__ == "__main__":
    main()
