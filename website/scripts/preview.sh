#!/usr/bin/env bash
# The site, with /stats from made-up figures, and the admin view from a
# made-up database, served on this computer for a look before a deploy -
# built in a container (preview.Dockerfile), so nothing but Docker is needed
# and every computer builds it alike. What it builds is the checkout's files
# as they are now, uncommitted changes and new files too (checkout_files):
# nothing git ignores, so no .env and no captures/. Ctrl-C stops it.
#   website/scripts/preview.sh
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=SCRIPTDIR/../../tools/repo/emulator-room.sh
. "$ROOT/tools/repo/emulator-room.sh"

IMAGE=firmware-liberator-website-preview
NAME=firmware-liberator-website-preview
SITE_PORT=4321
ADMIN_PORT=8788

# The context is the two files the image installs from, so a change to
# anything else does not rebuild it.
tar -C "$ROOT/website" -c package.json package-lock.json -C scripts preview.Dockerfile \
    | docker build -q -t "$IMAGE" -f preview.Dockerfile --build-arg BASE="$LOCAL_API_IMAGE" - >/dev/null

docker rm -f "$NAME" >/dev/null 2>&1 || true
# Published on this computer alone: the admin view is not for the network.
docker create --init --rm --name "$NAME" -p "127.0.0.1:$SITE_PORT:$SITE_PORT" -p "127.0.0.1:$ADMIN_PORT:$ADMIN_PORT" \
    -e NODE_OPTIONS=--disable-warning=ExperimentalWarning "$IMAGE" bash -c "set -e
        cd /repo && git init -q && git add -A
        cd website && node scripts/sample-stats.mjs > /tmp/stats.json
        STATS_FILE=/tmp/stats.json npx astro build --silent
        node scripts/admin-preview.mjs 0.0.0.0 $ADMIN_PORT &
        echo 'The site:       http://localhost:$SITE_PORT/  (/stats: http://localhost:$SITE_PORT/stats)'
        echo 'The admin view: http://localhost:$ADMIN_PORT/'
        exec npx astro preview --host 0.0.0.0 --port $SITE_PORT" >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
# The files git tracks or would, over the image's packages.
checkout_files "$ROOT" | tar -C "$ROOT" --null -T - --ignore-failed-read -c 2>/dev/null | docker cp -q - "$NAME:/repo"
docker start -a "$NAME"
