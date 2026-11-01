#!/bin/sh
# Deploy the web build to a Docker host and restart the container.
#   HOST=user@server SSH_KEY=/path/to/private/key ./scripts/deploy.sh
#   ALLOW_PRIVATE=1 MAX_STREAMS=8 ./scripts/deploy.sh   # optional overrides
#
# The image is pinned to VERSION (e.g. 1.6.12) and also tagged :latest, with the
# version and the checked-out git revision recorded as
# org.opencontainers.image.* labels so a deployed box can be traced back to a
# commit.  Bump VERSION, commit and tag vX.Y.Z on main before deploying.
#
# Rollback: the new image is built before the old container is touched, the old
# container is stopped (not removed) before the new one starts, and a failed
# health check restarts the old container and exits non-zero.  The data backup
# lives on the host next to DEST (never only in /tmp) until the new container is
# healthy, so a failed deploy cannot lose favourites.
set -eu
cd "$(dirname "$0")/.."
HOST="${HOST:?set HOST=user@server}"
KEY="${SSH_KEY:?set SSH_KEY=/path/to/private/key}"
DEST="${DEST:-/opt/raveplayer-web}"
PORT="${PORT:-8097}"
VERSION="$(cat VERSION 2>/dev/null || true)"
[ -n "$VERSION" ] || { echo "VERSION file is missing or empty" >&2; exit 1; }
REVISION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
IMAGE="raveplayer-web:$VERSION"
LATEST_IMAGE="raveplayer-web:latest"
ALLOW_PRIVATE="${ALLOW_PRIVATE:-0}"
MAX_STREAMS="${MAX_STREAMS:-6}"
RAVEWEB_ENV="${RAVEWEB_ENV:-production}"
WIDGET_TOKEN="${RAVEWEB_WIDGET_TOKEN:-}"

# 1.6.21 auth: the container now fails closed without these. Refuse before we
# touch the running box rather than letting the health check roll back.
[ -n "${SECRET_KEY:-}" ] || { echo "SECRET_KEY is required" >&2; exit 1; }
[ -n "${RAVEWEB_PASSWORD_HASH:-}" ] || { echo "RAVEWEB_PASSWORD_HASH is required" >&2; exit 1; }
[ -n "${RAVEWEB_FRIENDS_PASSWORD_HASH:-}" ] || { echo "RAVEWEB_FRIENDS_PASSWORD_HASH is required" >&2; exit 1; }

echo "deploying $IMAGE (git $REVISION), also tagging $LATEST_IMAGE"

# Write secrets to a mode-600 file outside the build context and copy it to the
# host. Never put them on an ssh command line or in the build context.
AUTH_ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/raveplayer-web-auth.XXXXXX")"
trap 'rm -f "$AUTH_ENV_FILE" /tmp/raveplayer-web.tgz' EXIT INT TERM
(umask 077
 {
   printf 'SECRET_KEY=%s\n' "$SECRET_KEY"
   printf 'RAVEWEB_PASSWORD_HASH=%s\n' "$RAVEWEB_PASSWORD_HASH"
   printf 'RAVEWEB_FRIENDS_PASSWORD_HASH=%s\n' "$RAVEWEB_FRIENDS_PASSWORD_HASH"
   printf 'RAVEWEB_ENV=%s\n' "$RAVEWEB_ENV"
   if [ -n "$WIDGET_TOKEN" ]; then
       printf 'RAVEWEB_WIDGET_TOKEN=%s\n' "$WIDGET_TOKEN"
   fi
 } > "$AUTH_ENV_FILE")
scp -q -i "$KEY" -o BatchMode=yes "$AUTH_ENV_FILE" "$HOST:/tmp/raveplayer-web-auth.env"

tar czf /tmp/raveplayer-web.tgz \
    --exclude='.venv' --exclude='.git' --exclude='test' --exclude='test_e2e' \
    --exclude='docs' --exclude='.pytest_cache' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='data' --exclude='AGENTS.md' --exclude='rave-player-space' -C . .
scp -q -i "$KEY" -o BatchMode=yes /tmp/raveplayer-web.tgz "$HOST:/tmp/raveplayer-web.tgz"

ssh -i "$KEY" -o BatchMode=yes "$HOST" "bash -s" <<REMOTE
set -eu
DATA_BACKUP="$DEST.data-backup"
# Secrets live in a root-only env file outside the build context; the EXIT trap
# guarantees it is removed on every path (success, run failure, rollback).
ENV_FILE=/tmp/raveplayer-web.env
AUTH_ENV=/tmp/raveplayer-web-auth.env
trap 'rm -f "\$ENV_FILE" "\$AUTH_ENV"' EXIT INT TERM
# keep favourites/cache on the host (not only in /tmp) until the new box is good
rm -rf "\$DATA_BACKUP"
if [ -d $DEST/data ]; then cp -a $DEST/data "\$DATA_BACKUP"; fi
rm -rf $DEST
mkdir -p $DEST
tar xzf /tmp/raveplayer-web.tgz -C $DEST 2>/dev/null
rm -f /tmp/raveplayer-web.tgz
mkdir -p $DEST/data
if [ -d "\$DATA_BACKUP" ]; then cp -a "\$DATA_BACKUP/." $DEST/data/; fi
chown -R 10001:10001 $DEST/data
cd $DEST
# Build first: a build failure leaves the running container untouched.
docker build -q \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$REVISION" \
  -t $IMAGE -t $LATEST_IMAGE .
# The old container is stopped, not removed, so it can be restarted on failure.
if docker inspect raveplayer-web >/dev/null 2>&1; then
  docker stop -t 20 raveplayer-web >/dev/null 2>&1 || true
fi
docker rm -f raveplayer-web-new >/dev/null 2>&1 || true
# One rollback path for every failure after the old container was stopped: a
# failed `docker run` used to abort under `set -eu` and leave the site down.
restore_previous() {
  echo "rolling back to the previous container"
  docker logs --tail 60 raveplayer-web-new 2>&1 || true
  docker rm -f raveplayer-web-new >/dev/null 2>&1 || true
  if docker inspect raveplayer-web >/dev/null 2>&1; then
    docker start raveplayer-web >/dev/null 2>&1 || true
  fi
  exit 1
}
(umask 077 && cat "\$AUTH_ENV" > "\$ENV_FILE" && printf 'SOUNDCLOUD_CLIENT_ID=%s\n' "${SOUNDCLOUD_CLIENT_ID:-}" >> "\$ENV_FILE")
rm -f "\$AUTH_ENV"
if ! docker run -d --name raveplayer-web-new --restart unless-stopped \
  --security-opt no-new-privileges:true \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$REVISION" \
  -p $PORT:$PORT \
  -v $DEST/data:/data \
  -e RAVEWEB_DATA=/data \
  -e RAVEWEB_ALLOW_PRIVATE=$ALLOW_PRIVATE \
  -e RAVEWEB_MAX_STREAMS=$MAX_STREAMS \
  --env-file "\$ENV_FILE" \
  $IMAGE >/dev/null; then
  restore_previous
fi
rm -f "\$ENV_FILE"
healthy=0
for i in \$(seq 1 20); do
  if curl -fsS -m 4 http://127.0.0.1:$PORT/healthz; then
    healthy=1
    break
  fi
  sleep 1
done
if [ "\$healthy" -ne 1 ]; then
  restore_previous
fi
# New container is healthy: promote it and drop the rollback target.
docker rm -f raveplayer-web >/dev/null 2>&1 || true
docker rename raveplayer-web-new raveplayer-web
rm -rf "\$DATA_BACKUP"
echo
echo "healthy after \${i}s - $IMAGE (revision $REVISION)"
docker ps --filter name=raveplayer-web --format '{{.Names}} {{.Status}}'
exit 0
REMOTE

rm -f /tmp/raveplayer-web.tgz
echo "deployed on $HOST (port $PORT): $IMAGE, revision $REVISION"
