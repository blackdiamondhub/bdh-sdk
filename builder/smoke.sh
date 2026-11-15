#!/usr/bin/env bash
# Smoke-test a hub app image the way the supervisor runs it:
#
#   builder/smoke.sh <image> [report.json]
#
# Optional: SMOKE_ENV_FILE (extra env for the app, docker --env-file format)
# and SMOKE_SETTLE (seconds to let background workers run before measuring;
# default 5).
#
# Non-root, read-only rootfs, all capabilities dropped, no-new-privileges,
# a fresh /data volume, a 64 MB /tmp tmpfs and the app's memory limit. The
# broker is builder/stub_broker.py (a hub with nothing configured), in a
# sidecar whose network namespace the app shares. Checks that the app
# migrates and starts, answers /_bdh/health with its id, and that its URLs
# and views import. Then it measures memory (RSS and PSS summed over the
# container's processes, and the cgroup's usage) and writes a JSON report.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
image=$1
report=${2:-}

app=$(docker image inspect -f '{{index .Config.Labels "org.blackdiamondlodge.bdh.app"}}' "$image")
[ -n "$app" ] || { echo "$image has no org.blackdiamondlodge.bdh.app label" >&2; exit 2; }
mem=$(docker run --rm --entrypoint python "$image" -c \
    'import json; print(json.load(open("/opt/bdh/app.json")).get("memory_mb") or 96)')
name="bdh-smoke-${app}-$$"
env_args=()
[ -z "${SMOKE_ENV_FILE:-}" ] || env_args=(--env-file "$SMOKE_ENV_FILE")
token=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

cleanup() {
    docker rm -f "$name" "$name-broker" >/dev/null 2>&1 || true
    docker volume rm -f "$name-data" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$name-data" >/dev/null
docker run -d --name "$name-broker" --user 10001:10001 --read-only --cap-drop ALL \
    -v "$here/stub_broker.py:/opt/stub_broker.py:ro" --entrypoint python "$image" \
    /opt/stub_broker.py 8765 >/dev/null

docker run -d --name "$name" --network "container:$name-broker" \
    --user 10001:10001 --read-only --init \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --pids-limit 256 --memory "${mem}m" --memory-swap "${mem}m" \
    --mount "type=volume,source=$name-data,target=/data" \
    -e BDH_APP_ID="$app" -e BDH_APP_TOKEN="$token" -e BDH_DATA_DIR=/data \
    -e BDH_BROKER_URL=http://127.0.0.1:8765/_broker/ -e BDH_HOST=0.0.0.0 -e BDH_PORT=8000 \
    -e BDH_TIME_ZONE=UTC -e BDH_HUB_BRAND='{"name":"Smoke"}' "${env_args[@]}" \
    --entrypoint python "$image" -m bdh_sdk.runtime >/dev/null

health='
import json, sys, time, urllib.request
deadline = time.monotonic() + 120
while True:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/_bdh/health", timeout=5) as r:
            body = json.load(r)
        break
    except Exception as e:
        if time.monotonic() > deadline:
            sys.exit(f"no health answer: {e}")
        time.sleep(1)
assert body == {"ok": True, "app": sys.argv[1]}, body
print("health:", body)
'
if ! docker exec "$name" python -c "$health" "$app"; then
    docker logs "$name" 2>&1 | tail -n 100
    exit 1
fi

docker exec "$name" python -c '
from bdh_sdk.runtime import app_spec, setup
setup()
import bdh_sdk.runtime.urls  # noqa: F401  (the app URLconf and its views)
from django.urls import get_resolver
get_resolver().url_patterns
print("urls: ok", app_spec().id)
'

# Unsigned requests must be refused; a few health probes warm the server.
docker exec "$name" python -c '
import urllib.error, urllib.request
for _ in range(10):
    urllib.request.urlopen("http://127.0.0.1:8000/_bdh/health", timeout=5).read()
try:
    urllib.request.urlopen("http://127.0.0.1:8000/", timeout=5)
    raise SystemExit("an unsigned request was served")
except urllib.error.HTTPError as e:
    assert e.code in (401, 403, 404), e.code
print("identity: unsigned request refused")
'
sleep "${SMOKE_SETTLE:-5}"
if docker logs "$name" 2>&1 | grep -E 'ImportError|ModuleNotFoundError|OperationalError'; then
    echo "unexpected errors in the container log" >&2; exit 1
fi
state=$(docker inspect -f '{{.State.Running}} {{.State.OOMKilled}}' "$name")
[ "$state" = "true false" ] || { echo "container not running (running/oom: $state)" >&2; docker logs "$name" 2>&1 | tail -n 60; exit 1; }

docker exec "$name" python -c '
import json, os
rss = pss = 0
for pid in filter(str.isdigit, os.listdir("/proc")):
    if int(pid) == os.getpid():
        continue  # this probe
    try:
        text = open(f"/proc/{pid}/smaps_rollup").read()
    except OSError:
        continue
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if k == "Rss":
            rss += int(v.split()[0])
        elif k == "Pss":
            pss += int(v.split()[0])
cg = int(open("/sys/fs/cgroup/memory.current").read())
stat = dict(line.split() for line in open("/sys/fs/cgroup/memory.stat"))
ws = cg - int(stat.get("inactive_file", 0))
print(json.dumps({"rss_mb": round(rss / 1024, 1), "pss_mb": round(pss / 1024, 1),
                  "cgroup_mb": round(cg / 2**20, 1), "working_set_mb": round(ws / 2**20, 1)}))
' > "$name.mem.json"

size=$(docker image inspect -f '{{.Size}}' "$image")
python3 - "$name.mem.json" "$app" "$image" "$size" "$mem" "$report" <<'PY'
import json, sys
mem = json.load(open(sys.argv[1]))
out = {"app": sys.argv[2], "image": sys.argv[3], "image_mb": round(int(sys.argv[4]) / 2**20, 1),
       "memory_limit_mb": int(sys.argv[5]), **mem}
print(json.dumps(out))
if sys.argv[6]:
    open(sys.argv[6], "w").write(json.dumps(out, indent=2) + "\n")
PY
rm -f "$name.mem.json"
if docker logs "$name" 2>&1 | grep -q '^Traceback'; then
    echo "::warning::$app logged a traceback during the smoke test:"
    docker logs "$name" 2>&1 | tail -n 60
fi
