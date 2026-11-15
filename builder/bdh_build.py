"""Helpers for building hub app images (stdlib only; see builder/README.md).

``python builder/bdh_build.py config --pyproject pyproject.toml --wheel W -o bdh-app.json``
    The app's build config from ``[tool.bdh]`` (``apt``, ``pip_exclude``,
    ``custom_base``) plus the distribution name and version.

``python builder/bdh_build.py lock --report report.json --config bdh-app.json -o bdh-lock.txt``
    Turn a ``pip install --dry-run --report`` (run in the base image against
    its constraints, so it only lists what the base doesn't have) into a
    hash-locked requirements file, minus the app and its ``pip_exclude``.

``python builder/bdh_build.py context --artifacts DIR -o CTX``
    Assemble a build context (wheel, lockfile, Dockerfile with the
    ``custom_base`` extra spliced in) and print its build args, one
    ``KEY=VALUE`` per line.

``python builder/bdh_build.py manifest --config bdh-app.json --app-json app.json``
    Merge the manifest the image build wrote (/opt/bdh/app.json) into
    bdh-app.json.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCKERFILE = HERE / "app.Dockerfile"
EXTRA_MARKER = "#BDH-EXTRA"
EXTRA_NAME = "bdh-extra.Dockerfile"
APT_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_name_version(wheel):
    parts = Path(wheel).name.split("-")
    return parts[0], parts[1]


def wheel_app_id(wheel):
    """The single ``blackdiamondhub.plugins`` entry point name in the wheel."""
    import configparser
    import zipfile

    with zipfile.ZipFile(wheel) as zf:
        names = [n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt")]
        if len(names) != 1:
            raise SystemExit(f"{wheel}: no entry_points.txt")
        parser = configparser.ConfigParser(delimiters=("=",))
        parser.optionxform = str
        parser.read_string(zf.read(names[0]).decode())
    ids = list(parser["blackdiamondhub.plugins"]) if parser.has_section("blackdiamondhub.plugins") else []
    if len(ids) != 1:
        raise SystemExit(f"{wheel}: expected one blackdiamondhub.plugins entry point, found {ids}")
    return ids[0]


def build_config(pyproject, wheel):
    import tomllib

    data = tomllib.loads(Path(pyproject).read_text())
    project = data.get("project", {})
    tool = data.get("tool", {}).get("bdh", {})
    unknown = set(tool) - {"apt", "pip_exclude", "custom_base"}
    if unknown:
        raise SystemExit(f"unknown [tool.bdh] keys: {', '.join(sorted(unknown))}")
    apt = list(tool.get("apt", []))
    for pkg in apt:
        if not APT_NAME.match(pkg):
            raise SystemExit(f"[tool.bdh] apt: {pkg!r} is not a package name")
    name, version = wheel_name_version(wheel)
    if project.get("version") and project["version"] != version:
        raise SystemExit(f"pyproject version {project['version']} != wheel version {version}")
    return {
        "id": wheel_app_id(wheel),
        "distribution": project.get("name", name),
        "version": version,
        "wheel": Path(wheel).name,
        "apt": apt,
        "pip_exclude": sorted(canonical(n) for n in tool.get("pip_exclude", [])),
        "custom_base": bool(tool.get("custom_base", False)),
    }


def lock_lines(report, config):
    skip = {canonical(config["distribution"]), *config.get("pip_exclude", [])}
    lines = []
    for item in report.get("install", []):
        meta = item["metadata"]
        name = canonical(meta["name"])
        if name in skip:
            continue
        info = item.get("download_info", {})
        sha = info.get("archive_info", {}).get("hashes", {}).get("sha256")
        url = info.get("url", "")
        if not sha:
            raise SystemExit(f"{name}: no sha256 in the pip report (url {url})")
        if not url.endswith(".whl"):
            raise SystemExit(f"{name}: not a wheel ({url})")
        lines.append(f"{name}=={meta['version']} --hash=sha256:{sha}")
    return sorted(lines)


def assemble(artifacts, out):
    artifacts, out = Path(artifacts), Path(out)
    config = json.loads((artifacts / "bdh-app.json").read_text())
    wheels = sorted(artifacts.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"{artifacts}: expected one wheel, found {len(wheels)}")
    if wheels[0].name != config["wheel"]:
        raise SystemExit(f"bdh-app.json names {config['wheel']}, found {wheels[0].name}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy2(wheels[0], out / wheels[0].name)
    shutil.copy2(artifacts / "bdh-lock.txt", out / "bdh-lock.txt")
    dockerfile = DOCKERFILE.read_text()
    extra = artifacts / EXTRA_NAME
    if config.get("custom_base"):
        snippet = extra.read_text() if extra.exists() else ""
        if re.search(r"^\s*FROM\s", snippet, re.I | re.M):
            raise SystemExit(f"{EXTRA_NAME} may not contain FROM")
        dockerfile = dockerfile.replace(EXTRA_MARKER, snippet)
    elif extra.exists():
        raise SystemExit(f"{EXTRA_NAME} is only allowed with [tool.bdh] custom_base = true")
    (out / "Dockerfile").write_text(dockerfile)
    (out / ".dockerignore").write_text("Dockerfile\n.dockerignore\n")
    return {
        "APP_ID": config["id"],
        "APT": " ".join(config.get("apt", [])),
        "PIP_EXCLUDE": ",".join(config.get("pip_exclude", [])),
        "APP_VERSION": config["version"],
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("config")
    c.add_argument("--pyproject", required=True)
    c.add_argument("--wheel", required=True)
    c.add_argument("-o", "--output", required=True)
    lk = sub.add_parser("lock")
    lk.add_argument("--report", required=True)
    lk.add_argument("--config", required=True)
    lk.add_argument("-o", "--output", required=True)
    cx = sub.add_parser("context")
    cx.add_argument("--artifacts", required=True)
    cx.add_argument("-o", "--output", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--config", required=True)
    m.add_argument("--app-json", required=True)
    args = p.parse_args(argv)

    if args.cmd == "config":
        config = build_config(args.pyproject, args.wheel)
        Path(args.output).write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    elif args.cmd == "lock":
        config = json.loads(Path(args.config).read_text())
        lines = lock_lines(json.loads(Path(args.report).read_text()), config)
        header = (f"# Third-party dependencies of {config['distribution']} {config['version']} that the\n"
                  "# bdh-app-runtime base doesn't provide. Generated by bdh-sdk builder/lock.sh.\n")
        Path(args.output).write_text(header + "".join(line + "\n" for line in lines))
    elif args.cmd == "context":
        build_args = assemble(args.artifacts, args.output)
        for key, value in build_args.items():
            print(f"{key}={value}")
    elif args.cmd == "manifest":
        config = json.loads(Path(args.config).read_text())
        manifest = json.loads(Path(args.app_json).read_text())
        if canonical(manifest.get("distribution", "")) != canonical(config["distribution"]):
            raise SystemExit(f"the image holds {manifest.get('distribution')}, not {config['distribution']}")
        if manifest.get("id") != config["id"]:
            raise SystemExit(f"the image holds app {manifest.get('id')}, not {config['id']}")
        if manifest.get("version") != config["version"]:
            raise SystemExit(f"the image holds version {manifest.get('version')}, not {config['version']}")
        config["manifest"] = manifest
        Path(args.config).write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
