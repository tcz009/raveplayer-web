# Rave Player web — runbook

## Deploy
```
HOST=root@192.168.1.179 SSH_KEY=.../omv_key ./scripts/deploy.sh
```
Needs env: SECRET_KEY, RAVEWEB_PASSWORD_HASH, RAVEWEB_FRIENDS_PASSWORD_HASH,
RAVEWEB_WIDGET_TOKEN, RAVEWEB_ENV=production. Host copy: /root/raveplayer-web.env (0600 root).
The script builds before stopping, starts the new container aside, health-checks and promotes;
rollback container is kept.

### Env files are parsed literally — never `source`/`eval`
Password hashes contain `$` (bcrypt/scrypt records). `source env`, `. env`, or
`eval "$(cat env)"` makes the shell expand `$...` and silently corrupts the
hash, so login fails only after deploy. The safe pattern is byte-literal
`KEY=VALUE` parsing, preferably Docker's own `--env-file`:

```sh
# deploy.sh already writes the file with printf '%s' and runs:
docker run --env-file /root/raveplayer-web.env ... raveplayer-web:VERSION
```

If you must load it in a shell, do this instead of sourcing:

```sh
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  key=${line%%=*}; value=${line#*=}
  export "$key=$value"          # value is not re-expanded
done < /root/raveplayer-web.env
```

Do not `printenv`/echo the hash to check it. If you must compare, do it in a
private shell and never paste the value into chat or logs; the post-deploy
login smoke below is the real verification.

## Verify
- `curl http://192.168.1.179:8097/healthz` → `{"ok":true,"version":...}`
- `/` unauth → redirect to `/login`; `/api/menu` unauth → 401
- `Authorization: Bearer $RAVEWEB_WIDGET_TOKEN` → `/api/admin/stats` 200, masked sessions only

### Post-deploy owner login smoke (literal env)
Supply the owner plaintext from the password manager, not the env file (the
host only stores the hash). The CSRF field comes from the login page:

```sh
base=http://192.168.1.179:8097
jar=$(mktemp)
curl -fsS -c "$jar" "$base/login" -o /tmp/rave-login.html
csrf=$(sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' /tmp/rave-login.html | head -1)
curl -fsS -o /dev/null -b "$jar" -c "$jar" \
  --data-urlencode "csrf=$csrf" --data-urlencode "username=owner" \
  --data-urlencode "password=$RAVEWEB_OWNER_PLAINTEXT" "$base/login"
curl -fsS -b "$jar" "$base/api/admin/whoami"
# expected: {"ok":true,"identity":"owner","role":"admin"}
rm -f "$jar" /tmp/rave-login.html
```

A `401`/`403` here after a deploy means the env/hash was corrupted or the
password is wrong; do not debug by echoing the secret.

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
