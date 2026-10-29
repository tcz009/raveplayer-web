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
| Rinse FM | station | 692† | Marcus Nasty – Rinse FM 2008/09/03 (with Mak 10) | 3 | Ben Pearce - Rinse FM 26/09/15 w/ Hogdson | add both |
| Kool London | station | 17† | Kool London - 1993 - EASY D & STINGRAY | 3 | DJ Ruff Stuff w/ Dizzee Rascal, D Double E, Funsta, Skiba… | add both |
| Rude FM | station | 5† | DBR UK & Skeptical ft MC Fokus - Rude FM - 29.12.2011 | 3 | Reality & Beaky - Rude FM - Jan 1995 | add both |
| Weekend Rush | station | 3† | Dj Barron + DJ Naf Naf - Weekend Rush 92.5 FM (1991) | 3 | Devastate Central Connection Live Weekend Rush FM 13th Ju… | add both |
| Eruption FM | station | 2† | Clarky - Eruption FM 101.3 9th August 1997 | 3 | Demolition Cru - Eruption FM 101.3 - London - 30.10.94 | add both |
| Fantasy FM | station | 2† | 19xx @ Fantasy FM London | 3 | HaLo-iS on Fantasy FM. Show 95 07.04.25 | add both |
| Conflict FM | station | 0 | — | 3 | Conflict FM 88.4 - Paranoia, RP, Jacko, Rush Puppie [Hard… | mixcloud only |
| Demolition Cru | station | 0 | — | 3 | Demolition Cru Elevation & Reincarnation United for 94 23… | mixcloud only |
| Pulse FM | station | 0 | — | 3 | DJ Aphrodite Live on Pulse FM 90.6, a Sunday afternoon in… | mixcloud only |
| Passion FM | station | 0 | — | 2 | Passion FM, UK Garage, Pirate Radio Station London, 90s,… | mixcloud only |
| Sunrise FM | station | 0 | — | 2 | Ellis Dee - Sunrise FM, London, December 1988 | mixcloud only |
| Renegade Hardware | promotion | 94† | Spirit @ Renegade Hardware XX Dürlin 2015.12.26 | 3 | Andy C & SP:MC Renegade Hardware @The End 2002 | add both |
| Innovation | promotion | 79† | Skibadee's Bangers & Mash Birthday Bash - Innovation - 31… | 3 | Hype skibba shabba fearless & det - Innovation the drum &… | add both |
| Tribal Gathering | promotion | 31† | Fluke @ Universe Tribal Gathering, Luton Hoo; Estate Bedf… | 3 | Mixmaster Morris @ Panama Tribal Gathering 2016 pt1 | add both |
| Metalheadz | promotion | 22† | Kemistry & Storm & Goldie @ Metalheadz, Brighton - 1996 | 3 | Goldie - Deviation Presents 'Metalheadz History Session' | add both |
| Club Kinetic | promotion | 13† | 1993-05-28 - Carl Cox feat. Connie @ Kinetic Promotions -… | 3 | DJ Slipmatt - Club Kinetic - Leisurebowl, Stoke on Trent… | add both |
| Warning | promotion | 13† | One Nation and Warning 31.3.00 | 2 | NICKY BLACKMARKET B2B RAY KEITH - MC STEVIE HYPER D - WAR… | add both |
| Pandemonium | promotion | 11† | 1992-06-13 @ Pandemonium - Andromeda III (The Trilogy) | 3 | Mickey Finn - Pandemonium Andromeda 'The Giants of Jungle… | add both |
| Spiral Tribe | promotion | 7† | Spiral Tribe / Person Unknown - Black Plastik | 3 | Spiral Tribe liveset live at Sterns Club  June 1993 | add both |
| Sky Blue | promotion | 6† | Doc Scott - Amnesia House - Sky Blue Connexion (1990) | 3 | Doc Scott - Amnesia House, Sky Blue, Aug 1990 | add both |
| Jungle Mania | promotion | 4† | Kenny Ken - Jungle Mania - Bday Jamboree - 1995 | 3 | DJ Hazard w/ Skibadee, Funsta & Det - Breakin Science mee… | add both |
| Castlemorton | promotion | 2† | Castlemorton Free Festival (UK) 1992 - DJ JACK (Cassette… | 3 | DIY Digs & Woosh @ Castle Morton 1992 | add both |
| Vision | promotion | 2 | Vision - Return of the Warehouse Concept pt 1 - 12.02.93 | 3 | Ellis Dee Vision 'The Return of the Warehouse Concept Par… | add both |
| Hysteria | promotion | 2 | Hysteria and Pure X pres. Junglemania Vol.2 | 2 | Andy C w/ Bassman, Hooligan, Palmer Charmer & Stevie Hype… | add both |
| Pure X | promotion | 2 | Hysteria and Pure X pres. Junglemania Vol.2 | 0 | — | archive only |
| Biology | promotion | 0 | — | 2 | Trevor Fung & Kid Batchelor - Biology presents Memories -… | mixcloud only |
| Exodus | promotion | 0 | — | 2 | Exodus Collective Luton Illegal Rave Live from mid 1990's… | mixcloud only |
| Perception | promotion | 0 | — | 2 | Sasha - (1991.xx.xx) Live @ Perception Rave | mixcloud only |
| Universe | promotion | 0 | — | 2 | DJ Ratty w/ MC Ribbz - Universe 'Big Love' - Lower Pertwo… | mixcloud only |
| Orange | promotion | 0 | — | 1 | DJ Randall Orange @ The Rocket Complex 1992 | mixcloud only |
| The Sanctuary | venue | 29† | Accelerated Culture 1 – The Sanctuary, Milton Keynes – 12… | 2 | Darren Jay & Stevie Hyper D - Helter Skelter 'Anthology'… | add both |
| Bagleys | venue | 11† | Innovation - 6th Birthday Valentine Special - Bagleys - 1… | 3 | DJ Randall w/ Stevie Hyper D, Skibadee & Fearless - Slamm… | add both |
| Turnmills | venue | 6† | Blu Peter @ FF, Turnmills, London (February 19, 1995) Par… | 3 | Paul van Dyk - [Essential Mix] Live @ Gallery Turnmills,… | add both |
| Que Club | venue | 5† | [BBC R1] Radio 1's Essential Mix: Paul Oakenfold, Pete To… | 3 | Sasha Live @ Que Club, Birmingham - Essential Mix - 1995-… | add both |
| The Rocket | venue | 4† | One Nation – The Birthday Ball 1994 – 1st Anniversary – T… | 0 | — | archive only |
| Sterns | venue | 3 | Carl Cox & Loftgroover @ In-Ter-Dance, Sterns, Worthing (… | 3 | Carl Cox - Sterns, Worthing - 1990 | add both |
| Roller Express | venue | 2† | One Nation – Under A Groove 1993 – Roller Express – Origi… | 3 | DJ Randall w/ Shabba, Hyper D, Sting and Det - Telepathy… | add both |
| The Astoria | venue | 2† | One Nation – The Midsummer Ball 1994 – New London Astoria… | 3 | Micky Finn - One Nation 'Midsummer Ball' - New London Ast… | add both |
| The End | venue | 2 | Cari Lekebusch @ The End, London (April 1999) | 3 | JAY MAJIK LIVE @ PLANET V- THE END CLUB LONDON | add both |
| The Manor | venue | 2† | Carl Cox @ The Manor (Ringwood), Bournemouth, UK (July 25… | 0 | — | archive only |
| 4 Aces | venue | 1† | Ragga Twins + MC Navigator Unity 4 Aces Dalston Lane 1988 | 3 | Billy 'Daniel' Bunter - Labrynth - 4 Aces club, 12 Dalsta… | add both |
| Bowlers | venue | 1 | Carl Cox - Fantazia Live At Bowlers (1994) | 3 | Si Frater - Bowlers Freedom Rave - 24.07.21 - B.E.C. Manc… | add both |
| Velvet Underground | venue | 1† | Jim Masters & Carl Cox @ 3 Years Ultimate Base, Velvet Un… | 2 | Chudd Velvet Underground London 13 5 95 | add both |
| Blue Note | venue | 0 | — | 3 | Bailey & MC Justyce - Metalheadz Blue Note Sessions - 12t… | mixcloud only |
| Club 414 | venue | 0 | — | 3 | DJ Rap - Jungle Nation - Club 414, Brixton - 29.2.94 | mixcloud only |
| Club Colosseum | venue | 0 | — | 3 | MARTIN LARNER & MC RANKING - EXPOSURE VOL 2, CLUB COLOSSE… | mixcloud only |
| Eclipse | venue | 0 | — | 3 | THE ECLIPSE COVENTRY - DJ SASHA 1991 | mixcloud only |
| Labrynth | venue | 0 | — | 3 | Billy Daniel Bunter - Labrynth after show & Rave Story wa… | mixcloud only |
| Lazerdrome | venue | 0 | — | 3 | Doc Scott - Shelleys 'Lazerdrome, Stoke on Trent - 24.10.… | mixcloud only |
| Leisure Lounge | venue | 0 | — | 3 | Kemistry & Storm w/MC Justyce & Cleveland Watkiss - Metal… | mixcloud only |
| Mr B's | venue | 0 | — | 3 | Ratpack w/ Evenson Allen - Pandemonium - Mr B's , Wolverh… | mixcloud only |
| Pleasuredome | venue | 0 | — | 3 | Fergus Natz Pleasuredome 95 atomix | mixcloud only |
| Shelley's | venue | 0 | — | 3 | DJ Sasha @ Shelleys, Stoke - 8th Feb 1991 | mixcloud only |
| The Institute | venue | 0 | — | 3 | DJ SILK LIVE @ BASHMENT PARTY @ 02 INSTITUTE BIRMINGHAM J… | mixcloud only |
| The Island | venue | 0 | — | 3 | Nicky Blackmarket & Stevie Hyper D  - Roast 'Valentines P… | mixcloud only |

## Already covered (no addition)

| Name | Type | Archive count (ravetapes) | Archive example | Mixcloud count | Mixcloud example | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Kool FM | station | 8 | Brockie & Fabrok - Kool FM 94.5 (23.4.93) | 3 | Mampi Swift & Skibadee - Kool FM 945 - 12.10.97 | skip — already covered |
| Dreamscape | promotion | 3 | Dreamscape 3 - Absolutely No Compromise 10.3.92 | 1 | 96-98 Dreamscape Happy Hardcore set. DJ Sonic | skip — already covered |
| Helter Skelter | promotion | 1 | Helter Skelter - Energy 97 - Pack 1 | 1 | DJ Hype w/ MC GQ & MC MC - Helter Skelter 'Energy 96' - 1… | skip — already covered |
| Fantazia | promotion | 1 | Carl Cox - Fantazia Live At Bowlers (1994) | 3 | DJ Stu Allan - Fantazia @ Bowlers | skip — already covered |
| One Nation | promotion | 3 (67†) | One Nation and Warning 31.3.00 | 1 | Brockie w/ Skibadee & Hyper D - One Nation Clash of the T… | skip — already covered |
| Centreforce | station | 0 | nothing in ravetapes (revived-station uploads only) | 3 | Jeremy Healy Debut Show - 88.3 Centreforce radio - 2020 | skip — already covered |
| Don FM | station | 0 (3†) | Don FM December 1996 Hedgerley Bucks | 3 | Rugged Vinyl Crew - Don FM 105.7 - London - 1993 | skip — already covered |
| Slammin' Vinyl | promotion | 0 (7†) | Brockie B2B Mampi Swift - Live at Slammin Vinyl | 3 | DJ Randall w/ Stevie Hyper D - Slammin Vinyl - Bagleys -… | skip — already covered |
| Jungle Fever | promotion | 2 | Jungle Fever Showtime 1995 | 3 | Brockie With Skiba, Shabba N Det @ Jungle Fever 2000 | skip — already covered |
| Roast | promotion | 2 | Roast - Sunday Session Ting 97 | 3 | Nicky Blackmarket & Stevie Hyper D - Roast - The Island,… | skip — already covered |
| Telepathy | promotion | 1 | Telepathy NYE Extravaganza 1994 PT 2 | 3 | Telepathy Jungle Dons 'Dub Plate Special' 1995 | skip — already covered |
| World Dance | promotion | 2 | World Dance - April 2nd, 1994 | 2 | DJ Hype w/ MC GQ - World Dance - Lydd Airport - 5.11.94 | skip — already covered |
| AWOL | promotion | 1 | AWOL '93 | 3 | 93-94 AWOL Jungle Tribute Mix | skip — already covered |
| Raindance | promotion | 0 | nothing in ravetapes | 3 | Kenny Ken w/ Shockin B & MC Moose - Raindance Summer Rave… | skip — already covered |
| Amnesia House | promotion | 0 (36†) | Amnesia House - Institute (Birmingham) - 1.5.94 | 3 | LTJ Bukem - Amnesia House The Big Bank Holiday Bash I - 1… | skip — already covered |
| Rezerection | promotion | 1 (10†) | Blu Peter @ Rezerection: Lurrrve Special, Edinburgh (1994) | 3 | Grooverider & Marc Smith @ Rezerection - The Event | skip — already covered |
| Vibealite | promotion | 0 (6†) | LTJ Bukem & MC Conrad Live @ Vibealite, Venue 44, Mansfie… | 3 | Mark EG - Vibealite 4th Birthday Party 1997 | skip — already covered |
| Hardcore Heaven | promotion | 0 (16†) | Hardcore Heaven 1 [CD1/2/3] | 3 | DJ Sy with Sharkey & Charlie B at Hardcore Heaven The Ret… | skip — already covered |
| Accelerated Culture | promotion | 0 (40†) | Accelerated Culture 1 - The Sanctuary, Milton Keynes - 12… | 3 | Andy C & Mampi Swift w/ MC's - Accelerated Culture - D&B… | skip — already covered |
| Seduction | promotion | 0 | nothing in ravetapes | 3 | DJ Seduction - Dreamscape 2 - The Sanctuary - 28.2.92 | skip — already covered |
| Elevation | promotion | 0 | nothing in ravetapes | 3 | DJ Rap w/ MC MC & Stevie Hyper D - Elevation 'The Story C… | skip — already covered |
| Garage Nation | promotion | 0 (5†) | Various Artists - Garage Nation: V2 Crew (c.2000) | 3 | DJ EZ Garage Nation 'Easter Special' April 1999 | skip — already covered |
| One In The Jungle | promotion | 0 (16†) | 4 Hero - One In The Jungle - 11.10.1996 | 3 | Ed Rush and Trace - BBC Radio One in the Jungle - 07.02.97 | skip — already covered |
| Fabric | venue | 0 | nothing | 3 | Transitions with John Digweed live from Fabric, London | skip — already covered |
| Ministry of Sound | venue | 0 | nothing | 3 | Club Classics Mini Mix / Ministry of Sound | skip — already covered |

## Skipped — no content, ambiguous or false positives

Counts are the raw best-case numbers observed, before rejection; they show why the name
looked tempting.

| Name | Type | Archive count | Archive example | Mixcloud count | Mixcloud example | Reason |
| --- | --- | --- | --- | --- | --- | --- |
| Sun City | promotion | 0 | — | 0 | — | no content in either source |
| Living Dream | promotion | 3† | Dreamscape XXII - The Living Dream (20.7.96) | 3 | DJ Dougal Dreamscape 22 'The Living Dream' | not a promotion: subtitle of Dreamscape 22 |
| Club Labyrinth | promotion | 0 | — | 2 | DJ Seduction - Dreamscape Rave Nation Labyrinth - Pleasur… | one-off Dreamscape event name, not a promotion |
| The Big Love | promotion | 0 | — | 3 | Universe 'Big Love' - Lower Pertwood Farm 13.8.93 | Universe event name; covered by Universe |
| DiY | promotion | 0 | — | 3 | DIY Rave Sessions - Independence Day | 'DIY' matches home improvement; Mixcloud hits are modern generic rave nights |
| Bedlam | promotion | 0 | — | 0 | — | no content |
| Feel Real | promotion | 1 | Carl Cox & MC Man Parris @ ESP Supernova | 0 | — | archive hit is a different event; no Mixcloud |
| Freedom | promotion | 7 | Anoebis - The History of Goa Trance: 1995 | 3 | FREEDOM RAVE 22.04.2015 | matches religious/US rave content |
| Energy | promotion | 25 | Helter Skelter - Energy 97 | 3 | Dj E 4 Energy - Rave On | matches everything; 'Energy 97' is a Helter Skelter event |
| Hardcore Takeover | promotion | 1 | beatmania IIDX soundtrack | 3 | Happy Hardcore Takeover - Energy1058 2026 | generic modern mix-series title |
| Kaos | promotion | 1 | Carl Cox - Kaos 14 Promo Mix (1992) | 3 | Get Down To The Underground - Kaos Rave | thin and generic: one tape only |
| Speed | promotion | 20 | Billy Nasty @ New York Club, Montpellier | 0 | — | generic word; the Speed nights are not tagged |
| Movement | promotion | 17 | Carl Cox @ MayDay Sonic Empire | 3 | Movement - Perpetual Drum & Bass Motion | generic; Mixcloud hits are a different mix series |
| Swerve | promotion | 0 | — | 2 | Swerve Inspired Drum & Bass | generic; no scene content |
| Hardware | promotion | 7 | Ash-A-Tack - Blackmarket Jungle Studio Mix | 3 | Renegade Hardware @ The End | better covered as Renegade Hardware |
| Funktion | promotion | 0 | — | 3 | Liquid Funktion @ The Annexe | generic; hits are unrelated / German-language |
| Bad Company | promotion | 0 | — | 3 | Bad Company Drum and Bass Classics | matches Bible and video-game content |
| Club UK | venue | 1 | Billy Nasty @ The Final Frontier Club, UK | 3 | Game Club UK - Episode 22 | phrase too generic to scope |
| The Forum | venue | 0 | — | 0 | — | no content; Kentish Town Forum sets are not tagged |
| Mr C's | venue | 0 | — | 3 | Kitty & Mr. C's Enchanted Tiki Hut Show | matches an unrelated podcast |
| The Cross | venue | 0 | — | 0 | — | no content |
| Rush | station | 31 | Brainkillers - Weekend Rush 92.3 | 3 | Devastate Live Weekend Rush FM | substring of Weekend Rush; no separate Rush FM |
| Thunder | station | 6 | Billy Nasty - House Masters: Hard House | 3 | Thunder FM | generic word; results unrelated |
| Flashback | station | 1 | Andrew Weatherall @ The Hacienda | 3 | Flashback FM 95.6 (GTA III) | generic; Mixcloud hits are a GTA radio station |
| Radar | station | 1 | Adam Beyer @ former US radar station | 3 | Radar FM Wroclaw | generic; non-UK hits |
| Upfront | station | 0 | — | 3 | Upfront Dance FM Brighton 2025 | modern Brighton station; no 90s airchecks |
| The Hacienda | venue | 1 | Andrew Weatherall @ The Hacienda (July 1993) | 0 | — | one set only; Mixcloud hits are podcasts about the club |
| LWR | station | 6 | Tim Westwood LWR April 1985 | 3 | PAULO LABELLO live on LWR 2022 | pre-1989 and modern shows; not a rave station |

## Notes for wiring

- `resources/rave-catalog.json` contains the 55 recommended additions only, with the exact
  query strings that produced the counts above (`archive_count` / `mixcloud_count`).
- `archive_query: null` means the archive had nothing usable; `mixcloud_query: null` means
  no Mixcloud query produced a usable hit.
- Counts are a snapshot of the two public APIs on 2026-10-29 and will drift.
- Nothing in `app.py`, `filters.json`, `test/` or `static/` was modified.
