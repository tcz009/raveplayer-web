# Per-identity favourites and Downloads — 1.6.26

`owner` and `friends` now have separate favourites and Downloads libraries.
Reads/writes use the signed-in session identity only, so two people sharing the
LAN app no longer overwrite each other's lists.

## Layout and migration

- **Favourites**: the pre-1.6.26 global `favourites.json` is adopted as the
  **owner's** store in place. Friends get
  `<RAVEPLAYER_FAV_DIR>/friends/favourites.json` when they save their first
  entry. Nothing is deleted or moved.
- **Downloads**: the pre-1.6.26 global `downloads/` directory (files +
  `index.json`) is adopted as the **owner's** library in place. Friends get
  `downloads/friends/` with their own files and index.
- `core/favourites.migrate_legacy()` / `core/downloads.migrate_legacy()` are
  idempotent validation/count helpers; running them repeatedly does not move or
  duplicate data. Friends start with an empty library/favourites list.
- `downloads/jobs.json` remains the shared job-metadata file, but every job
  records its identity and a completed job registers its file into that
  identity's index only. Job temp names include job + identity so two users
  downloading the same URL cannot overwrite each other's partial file.

## API behaviour

| Route | Default | Owner `all=1` |
|---|---|---|
| `GET /api/favourites` | current identity only | owner + friends entries, each tagged `identity` |
| `POST /api/favourites` | current identity | ignored; writes own |
| `DELETE /api/favourites/<key>` | own entry; 404 if not own/absent | owner `all=1` does not widen deletes |
| `GET /api/downloads` | current identity only | owner + friends entries, tagged `identity` |
| `POST /api/downloads` | current identity library | writes own |
| `DELETE /api/downloads/<id>` | own entry; 404 if not own/absent | no cross-user delete |
| `GET /api/downloads/<id>/stream\|file` | own entry; 404 otherwise | no cross-user stream/file |
| `GET /api/downloads/jobs` | own jobs (owner sees all, as before) | n/a |
| `POST /api/downloads/jobs` | writes completed file into own library | writes own |

- Cross-user access is deliberately **404**, not 403, and never reveals whether
  the other identity owns the object.
- The player UI does not send `all=1`; it shows the signed-in identity's data.
  Owner-all is an API/operator view (metadata; cross-user playback/deletion is
  still 404).
- Storage caps (`RAVEWEB_DOWNLOAD_BYTES`, `..._TOTAL`, item count, free space)
  apply per identity; free disk space remains shared.
- Downloads/favourites are not exposed to the Control Centre; no Control Centre
  changes.

## Tests

`test/test_identity_scope.py` (6): favourites isolation + cross-user 404; owner
`all=1` visibility and friends' ignored `all`; legacy favourites adoption +
idempotent migration; downloads isolation + owner `all=1` + cross-user
stream/file/delete 404; legacy library adoption + idempotent migration;
friends background job completion lands only in the friends library. Existing
favourites/downloads suites still pass with owner defaults. Full suite: 360
offline, 21 e2e.
