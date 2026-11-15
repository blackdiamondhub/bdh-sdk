#!/usr/bin/env bash
# Build a hub app image from its release artifacts:
#
#   builder/build_app.sh <artifacts dir> <base image@sha256:...> <tag>
#
# The artifacts dir holds the wheel, bdh-lock.txt, bdh-app.json and (only
# for custom_base apps) bdh-extra.Dockerfile. The base must be pinned by
# digest (BDH_ALLOW_UNPINNED_BASE=1 allows a local tag, for tests). After
# the build, the manifest the image wrote (/opt/bdh/app.json) is merged
# into bdh-app.json.
set -euo pipefail
dir=$(cd "$1" && pwd)
base=$2
tag=$3
here=$(cd "$(dirname "$0")" && pwd)

case "$base" in
    *@sha256:*) ;;
    *) [ "${BDH_ALLOW_UNPINNED_BASE:-}" = 1 ] || { echo "base must be pinned by digest: $base" >&2; exit 2; } ;;
esac

ctx="$dir/.bdh-context"
args=()
while IFS= read -r kv; do
    args+=(--build-arg "$kv")
done < <(python3 "$here/bdh_build.py" context --artifacts "$dir" -o "$ctx")

docker build --pull=false --platform linux/amd64 --build-arg "BASE=$base" "${args[@]}" -t "$tag" "$ctx"
rm -rf "$ctx"

cid=$(docker create "$tag")
trap 'docker rm -f "$cid" >/dev/null' EXIT
docker cp "$cid:/opt/bdh/app.json" "$dir/.app.json"
python3 "$here/bdh_build.py" manifest --config "$dir/bdh-app.json" --app-json "$dir/.app.json"
rm -f "$dir/.app.json"
echo "built $tag"
