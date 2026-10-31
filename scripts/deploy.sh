#!/bin/sh
# Deploy the web build to a Docker host and restart the container.
#   HOST=user@server SSH_KEY=/path/to/private/key ./scripts/deploy.sh
#   ALLOW_PRIVATE=1 MAX_STREAMS=8 ./scripts/deploy.sh   # optional overrides
#
# The image is pinned to VERSION (e.g. raveplayer-web:1.6.6) and also tagged
# :latest, with the version and the checked-out git revision recorded as
# org.opencontainers.image.* labels so a deployed box can be traced back to a
# commit.  Bump VERSION, commit and tag vX.Y.Z on main before deploying.
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

echo "deploying $IMAGE (git $REVISION), also tagging $LATEST_IMAGE"

tar czf /tmp/raveplayer-web.tgz \
    --exclude='.venv' --exclude='.git' --exclude='test' --exclude='test_e2e' \
    --exclude='docs' --exclude='.pytest_cache' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='data' --exclude='AGENTS.md' --exclude='rave-player-space' -C . .
scp -q -i "$KEY" -o BatchMode=yes /tmp/raveplayer-web.tgz "$HOST:/tmp/raveplayer-web.tgz"

ssh -i "$KEY" -o BatchMode=yes "$HOST" "bash -s" <<REMOTE
set -e
# keep favourites across deploys
rm -rf /tmp/raveplayer-data
[ -d $DEST/data ] && cp -a $DEST/data /tmp/raveplayer-data
rm -rf $DEST
mkdir -p $DEST
tar xzf /tmp/raveplayer-web.tgz -C $DEST 2>/dev/null
rm -f /tmp/raveplayer-web.tgz
mkdir -p $DEST/data
[ -d /tmp/raveplayer-data ] && cp -a /tmp/raveplayer-data/. $DEST/data/ && rm -rf /tmp/raveplayer-data
chown -R 10001:10001 $DEST/data
cd $DEST
docker build -q \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$REVISION" \
  -t $IMAGE -t $LATEST_IMAGE .
docker rm -f raveplayer-web >/dev/null 2>&1 || true
docker run -d --name raveplayer-web --restart unless-stopped \
  --security-opt no-new-privileges:true \
  -p $PORT:$PORT \
  -v $DEST/data:/data \
  -e RAVEWEB_DATA=/data \
  -e SOUNDCLOUD_CLIENT_ID="${SOUNDCLOUD_CLIENT_ID:-}" \
  -e RAVEWEB_ALLOW_PRIVATE=$ALLOW_PRIVATE \
  -e RAVEWEB_MAX_STREAMS=$MAX_STREAMS \
  $IMAGE >/dev/null
for i in \$(seq 1 20); do
  if curl -fsS -m 4 http://127.0.0.1:$PORT/healthz; then
    echo
    echo "healthy after \${i}s - $IMAGE (revision $REVISION)"
    docker ps --filter name=raveplayer-web --format '{{.Names}} {{.Status}}'
    exit 0
  fi
  sleep 1
done
echo "health check failed - container logs:"
docker logs --tail 60 raveplayer-web || true
exit 1
REMOTE

rm -f /tmp/raveplayer-web.tgz
echo "deployed on $HOST (port $PORT): $IMAGE, revision $REVISION"
