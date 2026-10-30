#!/bin/sh
# Deploy the web build to server-host and restart the container.
#   SSH_KEY=<key-dir>/key-file ./scripts/deploy.sh
#   ALLOW_PRIVATE=1 MAX_STREAMS=8 ./scripts/deploy.sh   # optional overrides
set -eu
cd "$(dirname "$0")/.."
HOST="${HOST:-user@192.0.2.10}"
KEY="${SSH_KEY:-$HOME/.ssh/key-file}"
DEST=/opt/raveplayer-web
PORT="${PORT:-8097}"
IMAGE=raveplayer-web:1.0
ALLOW_PRIVATE="${ALLOW_PRIVATE:-0}"
MAX_STREAMS="${MAX_STREAMS:-6}"

tar czf /tmp/raveplayer-web.tgz \
    --exclude='.venv' --exclude='.git' --exclude='test' --exclude='docs' \
    --exclude='.pytest_cache' --exclude='__pycache__' --exclude='*.pyc' \
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
docker build -q -t $IMAGE .
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
    echo "healthy after \${i}s"
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
echo "deployed: http://192.0.2.10:$PORT"
