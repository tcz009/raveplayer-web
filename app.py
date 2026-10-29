# -*- coding: utf-8 -*-
"""Rave Player — self-hosted web build.

Same content sources as the Kodi add-on (Internet Archive, radio-browser,
podcast feeds, DNBRADIO, the surviving legacy sites), served as a small
Flask app so it can run in Docker on a home server.

Two things make the browser work where an add-on does not:
* a server-side stream proxy, because some sources are hotlink-protected and
  because mixed-content/CORS would otherwise block playback, and
* one JSON endpoint per list, so the UI stays a thin client.
"""

import io
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from core import archive, cache, dnbradio, favourites, feeds, latest, legacy, radio

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False

DATA_DIR = os.environ.get("RAVEWEB_DATA") or "/tmp/raveweb"
os.makedirs(DATA_DIR, exist_ok=True)

ALLOW_PRIVATE = os.environ.get("RAVEWEB_ALLOW_PRIVATE", "") == "1"
USER_AGENT = "RavePlayer-Web/1.0 (+https://github.com/tcz009/plugin.audio.raveplayer)"
ART = "/static/art/"

ROWS = 40

ARCHIVE_SECTIONS = [
    ("rave", "Rave Tape Packs", "collection:ravetapes AND mediatype:audio", "Rave tapes collection"),
    ("jungle", "Jungle / Drum & Bass", 'collection:ravetapes AND (jungle OR "drum and bass")', "Jungle and DnB"),
    ("hardcore", "Hardcore", "collection:ravetapes AND hardcore", "Hardcore sets"),
    ("house", "House & Garage", "collection:ravetapes AND (garage OR house)", "House and garage"),
    ("pirate", "Pirate Radio", 'collection:ravetapes AND (Kool OR Centreforce OR "Don FM")', "Pirate radio sets"),
    ("tapepacks", "All Tape Packs", '(jungle OR "drum and bass" OR hardcore OR house) AND "tape pack"', "Everything tagged tape pack"),
]

RADIO_SECTIONS = [
    ("oldskool", "Oldskool & Rave", "oldskool"),
    ("jungle", "Jungle", "jungle"),
    ("dnb", "Drum & Bass", "drum and bass"),
    ("hardcore", "Hardcore", "hardcore"),
    ("garage", "House & Garage", "garage"),
    ("breakbeat", "Breakbeat", "breakbeat"),
]

SECTIONS = [
    ("latest", "Latest", "rave.png", "Newest of everything"),
    ("kool", "Kool FM", "radio.png", "Kool FM live plus archive sets"),
    ("filters", "Quick Filters", "tapepacks.png", "Classic pack series"),
    ("live", "Live Radio", "radio.png", "Stations streaming now"),
    ("packs", "Tape Packs", "tapepacks.png", "The ravetapes collection"),
    ("podcasts", "Podcasts & Mixes", "jungle.png", "Present-day shows"),
    ("dnb", "DNB Radio", "hardcore.png", "Recent jungle/DnB shows"),
    ("years", "By Year", "house.png", "Tape packs 1987 onwards"),
    ("legacy", "Legacy Sites", "pirate.png", "The 2014 survivors"),
    ("favourites", "Favourites", "about.png", "What you saved"),
    ("search", "Search", "search.png", "Search the Archive"),
]


# --------------------------------------------------------------------------- helpers


def _filters():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filters.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return [entry for entry in data if isinstance(entry, dict) and entry.get("id")]


def _filter(key):
    return next((entry for entry in _filters() if entry["id"] == key), None)


def _track(label, sub, play_url, art=None, fav=None):
    item = {"label": label, "sub": sub, "play": play_url, "art": art or (ART + "radio.png")}
    if fav:
        item["fav"] = fav
    return item


def _folder(label, sub, api, art=None, fav=None):
    item = {"label": label, "sub": sub, "open": api, "art": art or (ART + "tapepacks.png")}
    if fav:
        item["fav"] = fav
    return item


def _year_label(doc):
    year = doc.get("year") or str(doc.get("date") or "")[:4]
    return str(year) if year else ""


# --------------------------------------------------------------------------- api: structure


@app.get("/api/menu")
def api_menu():
    return jsonify({"sections": [
        {"id": sid, "label": label, "icon": ART + icon, "sub": sub}
        for sid, label, icon, sub in SECTIONS
    ], "site": {"title": "Rave Player", "version": "1.0"}})


@app.get("/api/latest")
def api_latest():
    items = []
    for entry in latest.merge(limit=60):
        when = entry.get("when")
        stamp = when.strftime("%d %b %Y") if when else ""
        if entry["kind"] == "pack":
            items.append(_folder(entry["title"], "%s · %s" % (entry["source"], stamp),
                                 "/api/item/%s" % urllib.parse.quote(entry["identifier"]),
                                 archive.thumb(entry["identifier"]),
                                 {"kind": "pack", "target": entry["identifier"], "title": entry["title"]}))
        elif entry["kind"] == "episode":
            items.append(_track(entry["title"], "%s · %s" % (entry["source"], stamp), entry["url"],
                                ART + "radio.png",
                                {"kind": "stream", "target": entry["url"], "title": entry["title"]}))
        else:
            items.append(_track(entry["title"], "%s · %s" % (entry["source"], stamp), None,
                                ART + "jungle.png"))
            items[-1]["play_via"] = "/api/dnb/%s" % urllib.parse.quote(str(entry["id"]))
    return jsonify({"title": "Latest", "items": items})


@app.get("/api/filters")
def api_filters():
    return jsonify({"title": "Quick Filters", "items": [
        _folder(entry["name"], entry.get("plot", ""), "/api/filter/%s" % entry["id"])
        for entry in _filters()
    ]})


@app.get("/api/filter/<key>")
def api_filter(key):
    entry = _filter(key)
    if not entry:
        return jsonify({"error": "unknown filter"}), 404
    page = max(1, request.args.get("page", type=int) or 1)
    items = []
    if page == 1 and entry.get("live_name"):
        items.append(_track("▶ %s — live now" % entry["name"], "Live streams", None, ART + "radio.png"))
        items[-1]["play_via"] = "/api/filter/%s/live" % key
    docs, total = archive.search(entry["query"], page=page, rows=ROWS)
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        title = doc.get("title") or identifier
        items.append(_folder(title, _year_label(doc), "/api/item/%s" % urllib.parse.quote(identifier),
                             archive.thumb(identifier),
                             {"kind": "pack", "target": identifier, "title": title}))
    payload = {"title": entry["name"], "items": items}
    if total > page * ROWS:
        payload["next"] = "/api/filter/%s?page=%d" % (key, page + 1)
    return jsonify(payload)


@app.get("/api/filter/<key>/live")
def api_filter_live(key):
    entry = _filter(key)
    if not entry:
        return jsonify({"error": "unknown filter"}), 404
    stations = radio.stations_by_name(entry.get("live_name", ""), limit=20)
    items = []
    for station in stations:
        name, meta = radio.display(station)
        items.append(_track(name, meta, radio.stream_url(station), station.get("favicon") or ART + "radio.png"))
    return jsonify({"title": "%s — live" % entry["name"], "items": items})


@app.get("/api/live")
def api_live():
    return jsonify({"title": "Live Radio", "items": [
        _folder(label, tag, "/api/live/%s" % key, ART + "radio.png")
        for key, label, tag in RADIO_SECTIONS
    ]})


@app.get("/api/live/<key>")
def api_live_tag(key):
    section = next((entry for entry in RADIO_SECTIONS if entry[0] == key), None)
    if not section:
        return jsonify({"error": "unknown station group"}), 404
    stations = radio.stations(section[2], limit=100)
    items = []
    for station in stations:
        name, meta = radio.display(station)
        items.append(_track(name, meta, radio.stream_url(station), station.get("favicon") or ART + "radio.png"))
    return jsonify({"title": section[1], "items": items})


@app.get("/api/packs")
def api_packs():
    return jsonify({"title": "Tape Packs", "items": [
        _folder(label, sub, "/api/packs/%s" % key, ART + "tapepacks.png")
        for key, label, query, sub in ARCHIVE_SECTIONS
    ]})


@app.get("/api/packs/<key>")
def api_pack_list(key):
    section = next((entry for entry in ARCHIVE_SECTIONS if entry[0] == key), None)
    if not section:
        return jsonify({"error": "unknown section"}), 404
    page = max(1, request.args.get("page", type=int) or 1)
    docs, total = archive.search(section[2], page=page, rows=ROWS)
    items = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        title = doc.get("title") or identifier
        items.append(_folder(title, _year_label(doc), "/api/item/%s" % urllib.parse.quote(identifier),
                             archive.thumb(identifier),
                             {"kind": "pack", "target": identifier, "title": title}))
    payload = {"title": section[1], "items": items}
    if total > page * ROWS:
        payload["next"] = "/api/packs/%s?page=%d" % (key, page + 1)
    return jsonify(payload)


@app.get("/api/item/<path:identifier>")
def api_item(identifier):
    tracks = archive.audio_files(identifier)
    items = [
        _track(track["title"], _human_duration(track.get("length")), track["url"], archive.thumb(identifier),
               {"kind": "stream", "target": track["url"], "title": track["title"]})
        for track in tracks
    ]
    return jsonify({"title": identifier.replace("-", " "), "items": items,
                    "art": archive.thumb(identifier)})


@app.get("/api/podcasts")
def api_podcasts():
    items = []
    for entry in feeds.registry():
        try:
            count = len(feeds.episodes(entry["id"], limit=200))
        except Exception:
            count = 0
        items.append(_folder(entry["name"], "%d episodes" % count, "/api/podcast/%s" % entry["id"],
                             ART + "radio.png"))
    return jsonify({"title": "Podcasts & Mixes", "items": items})


@app.get("/api/podcast/<feed_id>")
def api_podcast(feed_id):
    entry = feeds.feed(feed_id)
    if not entry:
        return jsonify({"error": "unknown feed"}), 404
    items = [
        _track(episode["title"], (episode.get("published") or "")[:16], episode["url"], ART + "radio.png",
               {"kind": "stream", "target": episode["url"], "title": episode["title"]})
        for episode in feeds.episodes(feed_id)
    ]
    return jsonify({"title": entry["name"], "items": items})


@app.get("/api/dnb")
def api_dnb():
    items = [
        _track(show["title"], show.get("date", ""), None, ART + "jungle.png")
        for show in dnbradio.mixes()
    ]
    for item, show in zip(items, dnbradio.mixes()):
        item["play_via"] = "/api/dnb/%s" % urllib.parse.quote(str(show["id"]))
    return jsonify({"title": "DNB Radio", "items": items})


@app.get("/api/dnb/<mix_id>")
def api_dnb_resolve(mix_id):
    url = dnbradio.stream_url(mix_id)
    if not url:
        return jsonify({"error": "could not resolve mix"}), 404
    return jsonify({"url": url})


@app.get("/api/years")
def api_years():
    import datetime
    this_year = datetime.date.today().year
    items = [_folder(str(year), "Tape packs", "/api/year/%d" % year, ART + "tapepacks.png")
             for year in range(this_year, 1987, -1)]
    return jsonify({"title": "By Year", "items": items})


@app.get("/api/year/<int:year>")
def api_year(year):
    page = max(1, request.args.get("page", type=int) or 1)
    docs, total = archive.search(latest.year_query(year), page=page, rows=ROWS)
    items = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        title = doc.get("title") or identifier
        items.append(_folder(title, _year_label(doc), "/api/item/%s" % urllib.parse.quote(identifier),
                             archive.thumb(identifier),
                             {"kind": "pack", "target": identifier, "title": title}))
    payload = {"title": str(year), "items": items}
    if total > page * ROWS:
        payload["next"] = "/api/year/%d?page=%d" % (year, page + 1)
    return jsonify(payload)


@app.get("/api/legacy")
def api_legacy():
    return jsonify({"title": "Legacy Sites", "items": [
        _folder("RaveTapePacks.com", "Full tape packs", "/api/legacy/ravetapepacks", ART + "tapepacks.png"),
        _folder("JungleTapes.com", "Toronto jungle radio shows", "/api/legacy/jungletapes", ART + "jungle.png"),
        _folder("Toronto Rave Mixtape Archive", "1990s mixtapes", "/api/legacy/toronto", ART + "rave.png"),
    ]})


@app.get("/api/legacy/ravetapepacks")
def api_legacy_rtp():
    items = [_folder("%s (%d tapes)" % (entry["name"], len(entry["tracks"])), "RaveTapePacks.com",
                     "/api/legacy/ravetapepacks/%s" % urllib.parse.quote(entry["key"]))
             for entry in legacy.packs()]
    return jsonify({"title": "RaveTapePacks.com", "items": items})


@app.get("/api/legacy/ravetapepacks/<path:pack>")
def api_legacy_rtp_pack(pack):
    entry = next((item for item in legacy.packs() if item["key"] == pack), None)
    if not entry:
        return jsonify({"error": "unknown pack"}), 404
    items = [_track(track["name"], "Tape", legacy.playable(track["url"]), ART + "tapepacks.png",
                    {"kind": "stream", "target": legacy.playable(track["url"]), "title": track["name"]})
             for track in entry["tracks"]]
    return jsonify({"title": entry["name"], "items": items})


@app.get("/api/legacy/jungletapes")
def api_legacy_jt():
    items = [_folder(show["name"], "JungleTapes.com", "/api/legacy/jungletapes/%s" % urllib.parse.quote(show["key"]))
             for show in legacy.jungle_shows()]
    return jsonify({"title": "JungleTapes.com", "items": items})


@app.get("/api/legacy/jungletapes/<path:show_key>")
def api_legacy_jt_show(show_key):
    items = [_track(track["name"], "JungleTapes.com", track["url"], ART + "jungle.png",
                    {"kind": "stream", "target": track["url"], "title": track["name"]})
             for track in legacy.jungle_tracks(show_key)]
    return jsonify({"title": show_key.replace("-", " "), "items": items})


@app.get("/api/legacy/toronto")
def api_legacy_toronto():
    items = [_track(track["name"], "Toronto Rave Mixtape Archive", track["url"], ART + "rave.png",
                    {"kind": "stream", "target": track["url"], "title": track["name"]})
             for track in legacy.toronto_tracks()]
    return jsonify({"title": "Toronto Rave Mixtape Archive", "items": items})


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"title": "Search", "items": []})
    # Search the curated collection first — full-text Archive search is noisy —
    # then widen to all audio if the curated hit list is thin.
    docs, total = archive.search("%s AND %s" % (query, latest.RAVE_COLLECTION), rows=ROWS)
    if total < 10:
        wider, _ = archive.search("%s AND mediatype:audio" % query, rows=ROWS)
        seen = {doc.get("identifier") for doc in docs}
        docs += [doc for doc in wider if doc.get("identifier") not in seen]
    items = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        title = doc.get("title") or identifier
        items.append(_folder(title, _year_label(doc), "/api/item/%s" % urllib.parse.quote(identifier),
                             archive.thumb(identifier),
                             {"kind": "pack", "target": identifier, "title": title}))
    return jsonify({"title": "Search: %s" % query, "items": items})


# --------------------------------------------------------------------------- favourites


@app.get("/api/favourites")
def api_fav_list():
    entries = favourites.load()
    items = []
    for entry in entries:
        key = favourites.key(entry)
        base = {"fav_key": key}
        if entry.get("kind") == "pack":
            item = _folder(entry["title"], "Tape pack", "/api/item/%s" % urllib.parse.quote(entry["target"]),
                           archive.thumb(entry["target"]))
        else:
            item = _track(entry["title"], "Show", entry["target"], ART + "radio.png")
        item.update(base)
        items.append(item)
    return jsonify({"title": "Favourites", "items": items})


@app.post("/api/favourites")
def api_fav_add():
    payload = request.get_json(silent=True) or {}
    saved = favourites.add({
        "kind": payload.get("kind", "pack"),
        "target": payload.get("target", ""),
        "title": payload.get("title", ""),
    })
    return jsonify({"saved": saved})


@app.delete("/api/favourites/<key>")
def api_fav_remove(key):
    return jsonify({"removed": favourites.remove(key)})


# --------------------------------------------------------------------------- streaming


def _host_allowed(url):
    if ALLOW_PRIVATE:
        return True
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local:
            return False
    return True


@app.get("/api/stream")
def api_stream():
    """Proxy the audio so Referer-guarded files and CORS/mixed-content work."""
    target = request.args.get("u", "")
    if not target or not _host_allowed(target):
        return jsonify({"error": "blocked or missing url"}), 400

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    referer = request.args.get("ref") or "https://%s/" % urllib.parse.urlparse(target).hostname
    headers["Referer"] = referer
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:
        upstream = urllib.request.urlopen(urllib.request.Request(target, headers=headers), timeout=30)
    except urllib.error.HTTPError as exc:
        return jsonify({"error": "upstream %s" % exc.code}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    passthrough = {}
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if upstream.headers.get(header):
            passthrough[header] = upstream.headers[header]
    passthrough.setdefault("Content-Type", "audio/mpeg")
    passthrough["Cache-Control"] = "no-store"
    passthrough["Access-Control-Allow-Origin"] = "*"

    def generate():
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), status=upstream.status, headers=passthrough,
                    direct_passthrough=True)


# --------------------------------------------------------------------------- pages


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "version": app.config.get("RAVEWEB_VERSION", "1.0")})


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "Rave Player",
        "short_name": "Rave Player",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a0f",
        "theme_color": "#e8ff00",
        "icons": [{"src": ART + "icon.png", "sizes": "512x512", "type": "image/png"}],
    }), 200, {"Content-Type": "application/manifest+json"}


def _human_duration(value):
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%d:%02d" % (minutes, seconds)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8097")), threaded=True)
