# Hub app image builder

Every hub app (hub API 2 and later) runs as its own container image,
`ghcr.io/blackdiamondhub/bdh-app-<id>@sha256:…`. Each image is built
`FROM` the shared runtime base, `ghcr.io/blackdiamondhub/bdh-app-runtime:hub-api-<N>`
from bdh-supervisor, pinned by digest. The base provides Python 3.13, Django,
uvicorn, whitenoise, bdh-sdk, requests, websocket-client, the non-root user
(10001) and the entrypoint (`python -m bdh_sdk.runtime`). On top of the base,
each image adds the app's wheel and the third-party dependencies the base
doesn't already provide.

## Who does what

App repos publish files only. They build no images, have no GHCR push
rights and need no secrets. `.github/workflows/app-release.yml` publishes a
GitHub release containing:

| asset | what |
|---|---|
| `<dist>-<version>-py3-none-any.whl` (+ sdist) | the app |
| `bdh-lock.txt` | hash-locked third-party deps not in the base (`lock.sh`) |
| `bdh-app.json` | `[tool.bdh]` build config plus the manifest the image reports (`/opt/bdh/app.json`) |
| `bdh-extra.Dockerfile` | `custom_base` apps only |

A PEP 440 pre-release version (`X.Y.ZaN`, `bN`, `rcN`; see
`release_version.py`) is published as a GitHub pre-release, and the catalog
builder lists it in the `beta` channel. See *Beta releases* in the main
README.

Each asset's build provenance is attested. Before publishing, the workflow
builds the image and smoke-tests it (`app_artifacts.sh`). `app-ci.yml` does
the same on every PR (job `image`).

The platform does the actual build in one builder workflow,
`blackdiamondhub/bdh-app-catalog/.github/workflows/build.yml`. For each
app's latest release it:

1. verifies the release assets' attestations;
2. builds the image with `build_app.sh` against the current base digest;
3. runs `smoke.sh`;
4. pushes the image to `ghcr.io/blackdiamondhub/bdh-app-<id>`;
5. attests the image (signer identity: that workflow on `main`);
6. opens a single catalog PR.

A base bump rebuilds every app in one matrix and produces one catalog PR.
The supervisor pulls by digest and refuses an image in either case:

- the image has no attestation from the builder workflow;
- its layers don't start with the layers of an allowed `bdh-app-runtime` base.

## `[tool.bdh]` in pyproject.toml

```toml
[tool.bdh]
apt = ["libzbar0"]        # Debian packages, installed as root before the app
pip_exclude = ["av"]      # deps left out of the lock (and the image)
custom_base = false       # true: allow docker/bdh-extra.Dockerfile (flagged for human review in the catalog)
```

- **apt**: package names only; anything else is rejected.
- **pip_exclude**: drops the named distributions from the lock. Their own
  dependencies are still installed if nothing else excludes them. The app
  must cope with the distribution being absent (`pip check` ignores
  excluded names). Cameras uses this for PyAV, which uiprotect imports but
  never uses for anything cameras calls.
- **custom_base**: the contents of `docker/bdh-extra.Dockerfile` replace
  the `#BDH-EXTRA` marker in `app.Dockerfile`. They run as root after the
  dependencies are installed. The snippet may not contain `FROM`.
  Afterwards the template switches back to `USER 10001:10001` and still
  runs the checks below. In the catalog, the entry is marked
  `custom_base: true` and needs a human-reviewed PR.

Any other `[tool.bdh]` key is an error.

## What the template enforces

`app.Dockerfile`:

- installs the lock with `--require-hashes --only-binary=:all:` and
  `-c /opt/bdh/constraints.txt`, so the app can't move a base-owned
  distribution. The base's constraints are its own `pip freeze`;
- installs the app wheel with `--no-deps`;
- byte-compiles site-packages;
- runs `/opt/bdh/check.py app`, which:
  - runs `pip check`;
  - verifies that every base distribution still has its pinned version and
    unmodified RECORD hashes;
  - requires exactly one `blackdiamondhub.plugins` entry point whose
    `spec.id` matches its name and whose `api_version` matches the base;
  - imports the app's AppConfig;
  - writes the manifest to `/opt/bdh/app.json`;
- ends as `USER 10001:10001` with the base entrypoint. The build context is
  bind-mounted and never lands in a layer.

## Scripts

| script | |
|---|---|
| `bdh_build.py` | stdlib helpers: `config`, `lock`, `context`, `manifest` |
| `lock.sh <base> <dir>` | `pip install --dry-run --report` in the base against its constraints, then writes `bdh-lock.txt` |
| `build_app.sh <dir> <base@sha256:…> <tag>` | builds `linux/amd64` from the release assets in `<dir>` and merges the image's manifest into `bdh-app.json` |
| `smoke.sh <image> [report.json]` | runs the image the way the supervisor does, next to `stub_broker.py`, then measures memory |
| `app_artifacts.sh <wheel> <pyproject> <out> [base]` | all of the above, as the app workflows run them |

`smoke.sh` runs the app with the following settings:

- read-only rootfs;
- `cap_drop ALL` and no-new-privileges;
- user 10001;
- `/data` and tmpfs mounts;
- the memory limit from the manifest's `memory_mb` (default 96).

It runs the app beside a stub broker (`settings.get`, `state.*`,
`events.subscribe`, `hub.brand`) and then checks that:

- `/healthz` answers;
- the app's URLs import;
- an unsigned request is refused;
- the logs contain no import or migration errors;
- the container is still running and wasn't OOM-killed.

It then records RSS and PSS (summed over the container's processes) and the
cgroup working set. Pass extra environment variables with
`SMOKE_ENV_FILE=…`. Use `SMOKE_SETTLE=<s>` to give background workers time
before the memory is measured.
