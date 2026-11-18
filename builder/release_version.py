"""Release tag and channel for an app version (used by app-release.yml).

``python builder/release_version.py <version>`` prints ``KEY=VALUE`` lines
for ``$GITHUB_OUTPUT``:

    version=2.3.0b1
    tag=v2.3.0b1
    prerelease=true

A version is a beta iff it is a PEP 440 pre-release (``a``/``b``/``rc``). The
version must already be in normalized PEP 440 form, because the wheel's
filename (and so the catalog entry) carries the normalized form and the tag
must match it.
"""
import sys

from packaging.version import InvalidVersion, Version


def release_info(version):
    try:
        parsed = Version(version)
    except InvalidVersion:
        raise SystemExit(f"version {version!r} is not a valid PEP 440 version")
    if str(parsed) != version:
        raise SystemExit(
            f"version {version!r} is not in normalized PEP 440 form; use {str(parsed)!r}")
    if parsed.is_devrelease or parsed.local:
        raise SystemExit(f"version {version!r}: dev and local versions can't be released")
    return {"version": version, "tag": f"v{version}", "prerelease": parsed.is_prerelease}


def main(argv):
    if len(argv) != 1:
        raise SystemExit("usage: release_version.py <version>")
    info = release_info(argv[0])
    print(f"version={info['version']}")
    print(f"tag={info['tag']}")
    print(f"prerelease={'true' if info['prerelease'] else 'false'}")


if __name__ == "__main__":
    main(sys.argv[1:])
