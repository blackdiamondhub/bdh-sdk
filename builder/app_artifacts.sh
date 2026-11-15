#!/usr/bin/env bash
# Everything an app release publishes besides the wheel itself, checked by
# building and smoke-testing the image the platform will build from it:
#
#   builder/app_artifacts.sh <wheel> <pyproject.toml> <out dir> [base image]
#
# Writes to <out dir>: the wheel, bdh-app.json (build config + manifest),
# bdh-lock.txt, bdh-extra.Dockerfile (custom_base apps only, copied from
# docker/bdh-extra.Dockerfile next to pyproject.toml) and smoke.json, and
# leaves the image tagged bdh-app-<id>:candidate. The base defaults to
# ghcr.io/blackdiamondlodge/bdh-app-runtime:hub-api-${HUB_API:-2}, pulled and
# pinned by digest. SMOKE_ENV_FILE / SMOKE_SETTLE are passed to smoke.sh.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
wheel=$1
pyproject=$2
out=$3
base=${4:-ghcr.io/blackdiamondlodge/bdh-app-runtime:hub-api-${HUB_API:-2}}

case "$base" in
    *@sha256:*) ;;
    *)
        if [ "${BDH_ALLOW_UNPINNED_BASE:-}" != 1 ]; then
            docker pull -q "$base" >/dev/null
            base=$(docker image inspect -f '{{index .RepoDigests 0}}' "$base")
        fi
        ;;
esac
echo "base: $base"

mkdir -p "$out"
cp "$wheel" "$out/"
python3 "$here/bdh_build.py" config --pyproject "$pyproject" --wheel "$wheel" -o "$out/bdh-app.json"
extra="$(dirname "$pyproject")/docker/bdh-extra.Dockerfile"
[ ! -f "$extra" ] || cp "$extra" "$out/bdh-extra.Dockerfile"
bash "$here/lock.sh" "$base" "$out"
app=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$out/bdh-app.json")
BDH_ALLOW_UNPINNED_BASE=1 bash "$here/build_app.sh" "$out" "$base" "bdh-app-$app:candidate"
bash "$here/smoke.sh" "bdh-app-$app:candidate" "$out/smoke.json"
