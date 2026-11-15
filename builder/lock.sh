#!/usr/bin/env bash
# Lock an app's third-party dependencies against a bdh-app-runtime base:
#
#   builder/lock.sh <base image> <artifacts dir>
#
# The artifacts dir holds the app's wheel and bdh-app.json (from
# `bdh_build.py config`); this writes bdh-lock.txt next to them. pip
# resolves inside the base, against /opt/bdh/constraints.txt, so the lock
# lists only what the base doesn't already provide (and fails if the app
# needs a different version of something the base owns).
set -euo pipefail
base=$1
dir=$(cd "$2" && pwd)
here=$(cd "$(dirname "$0")" && pwd)
wheel=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["wheel"])' "$dir/bdh-app.json")

docker run --rm -u 0 --entrypoint pip -v "$dir":/a:ro "$base" \
    install --quiet --dry-run --only-binary=:all: -c /opt/bdh/constraints.txt \
    --report - "/a/$wheel" > "$dir/.pip-report.json"
python3 "$here/bdh_build.py" lock --report "$dir/.pip-report.json" --config "$dir/bdh-app.json" \
    -o "$dir/bdh-lock.txt"
rm -f "$dir/.pip-report.json"
cat "$dir/bdh-lock.txt"
