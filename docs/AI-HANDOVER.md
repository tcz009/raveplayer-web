# AI handover — Rave Player (read this first)

This note is for any AI or human who picks this project up later. It says what
the product is, what is live, how it is built/tested/deployed, and the working
rules. Secrets are never stored here.

## Mission

Rave Player is a 2026 rebuild of an old-skool rave archive: one full-screen Kodi
app (the Rave Screen) plus a web player, pulling tape packs, pirate-radio
recordings, live stations and mixes from public archives. It must stay fast,
quiet (no pop-ups), and legal-by-proxy: it streams from third-party public
sources and hosts nothing.

Two surfaces, one catalogue:
- **Kodi add-on** (`plugin.audio.raveplayer` + `script.raveplayer.ui` +
  `script.module.raveresolver`) — full-screen app is the primary shell; the
  Classic directory lists remain as fallback.
- **Web player** (Flask app, `Rave Player` repo) — LAN/Tailscale browser app
  with the same rooms, favourites and downloads.

## Current live state (as of this note)

- Kodi release: **v3.9.74** @ `47ec9a7` (plugin/screen 3.9.74, resolver 1.1.2)
  — installed on the Shield, GitHub release assets published.
- Web release: **v1.6.81** @ `471660d` — Docker/gunicorn on the NAS.
- Phase 0/1/2 certifications passed; reports in the workspace `docs/`:
  `PHASE0-*`, `PHASE1-*`, `PHASE2-*`.
- Web: LAN `http://192.168.1.179:8097`, Tailscale `http://100.93.238.43:8097`.
- Kodi device: Nvidia Shield `192.168.1.189` (ADB 5555, JSON-RPC 8080).

## Architecture in one screen

- `Rave Player/` — Flask app. Sources live in `core/` (`archive`, `bensons`,
  `ukraves`, `ezgarage`, `mikus`, `demodulated`, `feeds`, `radio`, `latest`,
  `moves`, favourites/downloads), resolver in `core/resolver/` (`/api/resolve`,
  Mega stream proxy, signed web playback `/api/webplay`), stream proxy
  `/api/stream`, auth/audit SQLite in `/data`.
- `raveplayer/` (Kodi repo) — plugin routes (`resources/lib/`), full-screen app
  (`script.raveplayer.ui/raveui/`), resolver module
  (`script.module.raveresolver/`, service fallback to the web `/api/resolve`).
- Web is the resolver/analysis server; Kodi can play direct, or ask the web for
  provider resolution/signed streams. `resolver_service_url` +
  `resolver_service_token` in plugin Settings connect the two.

## How to work on it

Web:
```
cd "Rave Player"
.venv/bin/python -m pytest test -q
./scripts/deploy.sh        # needs env; one deployer at a time
```
Kodi:
```
cd raveplayer            # or a worktree
python3 -m unittest discover -s test
./scripts/build.sh && ./scripts/build-repo.sh
./scripts/check-repo-artifacts.sh
```
Rule: one code owner per branch/worktree. Never edit a worktree someone else
owns. Never hand-edit `repo/` zips: rebuild them.
Release: commit → tag `v*` → push tag (GitHub Actions builds the release).

## Working agreements (important)

- **The Shield is off-limits without explicit owner approval.** Specialists must
  not touch Kodi/Shield. The Lead asks before any install/restart/settings
  change and claims `tools/shield-presence.sh` first.
- No pop-ups/warnings for the owner; RTP failures stay silent.
- Do not add video/YouTube/SoundCloud playback: hand-offs only.
- Downloads: direct/podomatic/20bensons/Toronto/JungleTapes/Mediafire/Drive/
  Mega-signed. Favourites on every playable row.
- One live version at a time; no deploy during certification.
- Certify before shipping: full suite twice, live identity equal start/end,
  device smoke.

## Known residuals (non-blocking)

- Kodi resolver: Classic `fav_list` still rebuilds saved streams as `mode=play`;
  limiter gates attempts not each HTTP request; service cache is per-process;
  Drive/MEGA typed errors have a default fallback flag.
- App absolute `Track.path` guard fails open if the plugin bridge is down
  (`web_play` re-checks); app `/file` favourites are remove-only.
- Web: no TLS origin on 8097 (service-worker secure-context behaviour was
  certified on a localhost secure context); slowest cold room is
  `/api/podcasts`; some old docs snapshots have historical counts.
- Cancel never deletes a completed duplicate (deliberate, documented).

## If you are an AI continuing this

1. Read this file, `AGENTS.md`, and the latest `PHASE*-CERT` report.
2. Check `git log --oneline -5` and tags in both repos; live `/healthz` must
   match the tag before trusting a deploy.
3. Use `tools/shield-presence.sh status|claim|release` for the Shield.
4. Test first, one change at a time, then certify exactly the shipped build.
5. Leave the Shield on Kodi Home, playback stopped, and write the result down.
