import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bdh_build", ROOT / "builder" / "bdh_build.py")
bdh_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdh_build)


def make_wheel(directory, entry_points="[blackdiamondhub.plugins]\ncams = cams.plugin:spec\n"):
    path = Path(directory) / "bdh_app_cams-1.2.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("bdh_app_cams-1.2.0.dist-info/entry_points.txt", entry_points)
    return path


def make_pyproject(directory, tool=""):
    path = Path(directory) / "pyproject.toml"
    path.write_text('[project]\nname = "bdh-app-cams"\nversion = "1.2.0"\n' + tool)
    return path


def report_item(name, version, url, sha="ab" * 32):
    info = {"url": url, "archive_info": {"hashes": {"sha256": sha}} if sha else {}}
    return {"metadata": {"name": name, "version": version}, "download_info": info}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wheel = make_wheel(self.tmp.name)

    def test_reads_tool_bdh(self):
        pyproject = make_pyproject(
            self.tmp.name, '[tool.bdh]\napt = ["libzbar0"]\npip_exclude = ["Py_AV"]\n')
        config = bdh_build.build_config(pyproject, self.wheel)
        self.assertEqual(config["id"], "cams")
        self.assertEqual(config["distribution"], "bdh-app-cams")
        self.assertEqual(config["apt"], ["libzbar0"])
        self.assertEqual(config["pip_exclude"], ["py-av"])
        self.assertFalse(config["custom_base"])

    def test_rejects_unknown_keys_and_bad_apt_names(self):
        for tool in ('[tool.bdh]\ndockerfile = "x"\n', '[tool.bdh]\napt = ["foo; rm -rf /"]\n'):
            with self.subTest(tool=tool), self.assertRaises(SystemExit):
                bdh_build.build_config(make_pyproject(self.tmp.name, tool), self.wheel)

    def test_rejects_version_mismatch(self):
        pyproject = Path(self.tmp.name) / "pyproject.toml"
        pyproject.write_text('[project]\nname = "bdh-app-cams"\nversion = "1.3.0"\n')
        with self.assertRaises(SystemExit):
            bdh_build.build_config(pyproject, self.wheel)

    def test_requires_exactly_one_plugin_entry_point(self):
        wheel = make_wheel(self.tmp.name, "[blackdiamondhub.plugins]\na = a:spec\nb = b:spec\n")
        with self.assertRaises(SystemExit):
            bdh_build.build_config(make_pyproject(self.tmp.name), wheel)


class LockTests(unittest.TestCase):
    config = {"distribution": "bdh-app-cams", "version": "1.2.0", "pip_exclude": ["av"]}

    def test_skips_the_app_and_excluded_and_sorts(self):
        report = {"install": [
            report_item("yarl", "1.0", "https://x/yarl-1.0-py3-none-any.whl", "11" * 32),
            report_item("bdh_app_cams", "1.2.0", "file:///w/bdh_app_cams-1.2.0-py3-none-any.whl"),
            report_item("av", "14.0", "https://x/av-14.0-cp313-manylinux.whl"),
            report_item("Aiohttp", "3.0", "https://x/aiohttp-3.0-cp313-manylinux.whl", "22" * 32),
        ]}
        self.assertEqual(bdh_build.lock_lines(report, self.config), [
            "aiohttp==3.0 --hash=sha256:" + "22" * 32,
            "yarl==1.0 --hash=sha256:" + "11" * 32,
        ])

    def test_rejects_unhashed_and_sdists(self):
        for item in (report_item("yarl", "1.0", "https://x/yarl-1.0-py3-none-any.whl", None),
                     report_item("yarl", "1.0", "https://x/yarl-1.0.tar.gz")):
            with self.subTest(item=item), self.assertRaises(SystemExit):
                bdh_build.lock_lines({"install": [item]}, self.config)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.art = Path(self.tmp.name) / "art"
        self.art.mkdir()
        make_wheel(self.art)
        (self.art / "bdh-lock.txt").write_text("")
        self.out = Path(self.tmp.name) / "ctx"

    def write_config(self, **extra):
        config = {"id": "cams", "version": "1.2.0", "wheel": "bdh_app_cams-1.2.0-py3-none-any.whl",
                  "apt": ["libzbar0"], "pip_exclude": ["av"], "custom_base": False, **extra}
        (self.art / "bdh-app.json").write_text(json.dumps(config))

    def test_build_args_and_unmodified_template(self):
        self.write_config()
        args = bdh_build.assemble(self.art, self.out)
        self.assertEqual(args, {"APP_ID": "cams", "APT": "libzbar0", "PIP_EXCLUDE": "av",
                                "APP_VERSION": "1.2.0"})
        self.assertEqual((self.out / "Dockerfile").read_text(), bdh_build.DOCKERFILE.read_text())

    def test_extra_only_with_custom_base(self):
        self.write_config()
        (self.art / bdh_build.EXTRA_NAME).write_text("RUN echo hi\n")
        with self.assertRaises(SystemExit):
            bdh_build.assemble(self.art, self.out)
        self.write_config(custom_base=True)
        bdh_build.assemble(self.art, self.out)
        dockerfile = (self.out / "Dockerfile").read_text()
        self.assertIn("RUN echo hi", dockerfile)
        # The template drops back to the unprivileged user after the extras.
        self.assertGreater(dockerfile.rindex("USER 10001:10001"), dockerfile.index("RUN echo hi"))

    def test_extra_may_not_replace_the_base(self):
        self.write_config(custom_base=True)
        (self.art / bdh_build.EXTRA_NAME).write_text("from debian\n")
        with self.assertRaises(SystemExit):
            bdh_build.assemble(self.art, self.out)


if __name__ == "__main__":
    unittest.main()
