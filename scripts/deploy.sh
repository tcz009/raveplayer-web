#!/bin/sh
# Deploy the web build to server-host and restart the container.
#   SSH_KEY=<key-dir>/key-file ./scripts/deploy.sh
set -eu
cd "$(dirname "$0")/.."
HOST="${HOST:-user@192.0.2.10}"
KEY="${SSH_KEY:-$HOME/.ssh/key-file}"
DEST=/opt/raveplayer-web
PORT="${PORT:-8097}"
IMAGE=raveplayer-web:1.0

tar czf /tmp/raveplayer-web.tgz \
    --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='data' \
    -C . .
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
  -p $PORT:$PORT \
  -v $DEST/data:/data \
  -e RAVEWEB_DATA=/data \
  $IMAGE >/dev/null
sleep 5
docker ps --filter name=raveplayer-web --format '{{.Names}} {{.Status}}'
curl -s -m 8 http://127.0.0.1:$PORT/healthz; echo
REMOTE

rm -f /tmp/raveplayer-web.tgz
echo "deployed: http://192.0.2.10:$PORT"
