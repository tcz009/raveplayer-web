<!--
Keep PRs small and verifiable: one topic, tests actually run, rollback known.
Fill every section; write "n/a - <reason>" when a section does not apply.
Do not paste tokens, session ids, private addresses or hostnames.
-->

## What / why

<!-- What this changes, why now, and the user-visible outcome. Link the board row or issue. -->

## Files / areas

<!-- Main paths or subsystems touched, e.g. `core/rave.py`, `app.py`, `static/app.js`, `static/app.css`. -->

## Contract impact

Tick every shared contract this PR touches, or tick "none".

- [ ] **Rave stats** - `/api/rave/stats`, `sessions_now`, `online_owner`, `friends`, session/history masking
- [ ] **Rave actions** - revoke / block / unblock / friends (owner controls), audit rows
- [ ] **Rave playback** - `/api/rave/playback`, heartbeat `source`, now playing / history windows
- [ ] **None** - no API shape, field, label or behaviour change

<!-- If ticked: old shape -> new shape, who consumes it, and which workspace must ship first. -->

## Tests run

<!-- Paste the command and its result line. "Tests pass" without a command is not evidence. -->

- [ ] `./.venv/bin/python -m pytest test -q` and `node --check static/app.js` - result:
- [ ] `bash -n` on any changed shell script
- [ ] Not run: <what and why>

## Screenshots

<!-- Before/after for UI changes; "n/a - backend only" otherwise. Keep addresses and ids out of the frame. -->

## Rollback

<!-- Exact revert path: `git revert <sha>`? previous image tag retained? Kodi zip version to reinstall? Any data migration to undo? -->

## Coordination

<!-- Copy the claim from docs/COORDINATION.md, or add one before merging. -->
| Area / files | Workspace | Branch | Status | ETA |
|---|---|---|---|---|
|  |  |  |  |  |

- [ ] Claim row added/updated in `docs/COORDINATION.md` (workspace board)
- [ ] Owning workspace review requested (CC owns `control-centre`; Rave owns `raveplayer` + `Rave Player`)
- [ ] Deploy is not part of this PR (deploys are recorded separately in the Deploys table)
- [ ] Rollback line filled in and reachable
