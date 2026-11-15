# The one way a hub app image is built (see builder/README.md).
#
# The build context (bind-mounted, so it never lands in a layer) is a directory holding exactly the app's release
# artifacts: one wheel, bdh-lock.txt (hash-locked third-party dependencies,
# without anything the base owns) and, for custom_base apps only,
# bdh-extra.Dockerfile, which build_app.sh splices in at the marker below.
# BASE must be bdh-app-runtime pinned by digest.
ARG BASE=bdh-app-runtime-base-not-set
FROM ${BASE}

ARG BASE
ARG APT=""
ARG PIP_EXCLUDE=""
ARG APP_ID
ARG APP_VERSION

USER root

# Declared system packages only (`[tool.bdh] apt = [...]`).
RUN if [ -n "${APT}" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends ${APT} \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Dependencies from the lockfile (exact versions and hashes, wheels only),
# then the app itself; neither may touch what the base pins.
RUN --mount=type=bind,target=/tmp/bdh-build \
    pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: \
        -c /opt/bdh/constraints.txt -r /tmp/bdh-build/bdh-lock.txt \
    && pip install --no-cache-dir --no-deps --only-binary=:all: \
        -c /opt/bdh/constraints.txt /tmp/bdh-build/*.whl

#BDH-EXTRA

USER root
RUN python -m compileall -q -j0 /usr/local/lib/python3.13/site-packages \
    && python /opt/bdh/check.py app --exclude "${PIP_EXCLUDE}" \
    && rm -rf /root/.cache

LABEL org.blackdiamondlodge.bdh.app="${APP_ID}" \
      org.blackdiamondlodge.bdh.version="${APP_VERSION}" \
      org.blackdiamondlodge.bdh.base="${BASE}"

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "bdh_sdk.runtime"]
CMD []
