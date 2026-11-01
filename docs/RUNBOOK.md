# Rave Player web — runbook

## Deploy
```
HOST=root@192.168.1.179 SSH_KEY=.../omv_key ./scripts/deploy.sh
```
Needs env: SECRET_KEY, RAVEWEB_PASSWORD_HASH, RAVEWEB_FRIENDS_PASSWORD_HASH,
RAVEWEB_WIDGET_TOKEN, RAVEWEB_ENV=production. Host copy: /root/raveplayer-web.env (0600 root).
The script builds before stopping, starts the new container aside, health-checks and promotes;
rollback container is kept.

## Verify
- `curl http://192.168.1.179:8097/healthz` → `{"ok":true,"version":...}`
- `/` unauth → redirect to `/login`; `/api/menu` unauth → 401
- owner login → `/api/admin/whoami` role admin; friends → 403 on admin
- `Authorization: Bearer $RAVEWEB_WIDGET_TOKEN` → `/api/admin/stats` 200, masked sessions only

## Rotate a password
Generate hash: `python3 -c "from core.auth import generate_hash; print(generate_hash('new'))"`.
Update the matching hash in the host env (owner: RAVEWEB_PASSWORD_HASH, friends: RAVEWEB_FRIENDS_PASSWORD_HASH),
redeploy. Sessions stay valid; to force re-login rotate SECRET_KEY.

## Invalidate all sessions (e.g. leaked session ids)
Rotate SECRET_KEY (openssl rand -hex 32), redeploy. Everyone signs in once.

## Rollback
Recreate the container from the retained previous image with the same mounts/ports/env,
or restore the stopped rollback container. Data: /data (favourites, downloads, auth.sqlite3).

## Tests
`pytest -q test` (342) and `pytest test_e2e -q` with system Chrome (20).
