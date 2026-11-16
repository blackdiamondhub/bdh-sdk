"""bdh_sdk.appdata / export_app_data / import_app_data, end to end in subprocesses."""
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.util import find_spec
from pathlib import Path

from bdh_sdk import appdata

ROOT = Path(__file__).resolve().parents[1]

PLUGIN = '''
from bdh_sdk.plugins import PluginSpec

PLUGIN = PluginSpec(id="notes", app="notesapp", urls="notesapp.urls")
'''
URLS = "urlpatterns = []\n"
MODELS = '''
from django.db import models


class Note(models.Model):
    text = models.CharField(max_length=50)
    blob = models.BinaryField(null=True)
'''
MIGRATION = '''
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Note",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("text", models.CharField(max_length=50)),
                ("blob", models.BinaryField(null=True)),
            ],
        ),
    ]
'''
SEED = '''
from bdh_sdk import runtime
runtime.setup()
from django.core.management import call_command
call_command("migrate", verbosity=0)
from notesapp.models import Note
Note.objects.create(text="hello", blob=b"\\x00\\xffpng")
Note.objects.create(text="world")
'''
CHECK = '''
import json
from bdh_sdk import runtime
runtime.setup()
from notesapp.models import Note
new = Note.objects.create(text="after")
print("RESULT" + json.dumps({
    "rows": [[n.pk, n.text, bytes(n.blob).hex() if n.blob is not None else None]
             for n in Note.objects.order_by("pk")],
    "new_pk": new.pk,
}))
'''


class _Chunked(io.StringIO):
    """A stream that records the largest read and refuses read() of everything."""

    def __init__(self, text):
        super().__init__(text)
        self.biggest = 0

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("whole-file read")
        self.biggest = max(self.biggest, size)
        return super().read(size)


class IterFixtureTests(unittest.TestCase):
    def rows(self, text, chunk_size=7):
        return list(appdata.iter_fixture(_Chunked(text), chunk_size=chunk_size))

    def test_streams_rows_in_small_reads(self):
        rows = [{"model": "a.b", "pk": i, "fields": {"s": "x]},{[\"" * i, "blob": "QUJD" * 50}}
                for i in range(20)]
        stream = _Chunked(json.dumps(rows, indent=1))
        self.assertEqual(list(appdata.iter_fixture(stream, chunk_size=16)), rows)
        # Reads grow to about one row, never to the whole file.
        self.assertLess(stream.biggest, len(json.dumps(rows[-1])) * 2 + 16)

    def test_empty_and_whitespace(self):
        self.assertEqual(self.rows("[]"), [])
        self.assertEqual(self.rows(' \n[ \n ]\n'), [])
        self.assertEqual(self.rows('[{"pk": 1}]\n'), [{"pk": 1}])

    def test_rejects_malformed(self):
        for bad in ["", "{}", "[1]", '[{"pk": 1} {"pk": 2}]', '[{"pk": 1}] x', '[{"pk": 1}',
                    '[{"pk": 1},']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.rows(bad)


@unittest.skipIf(find_spec("django") is None or find_spec("whitenoise") is None,
                 "runtime extra not installed")
class AppDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        pkg = base / "notesapp"
        (pkg / "migrations").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "plugin.py").write_text(PLUGIN)
        (pkg / "urls.py").write_text(URLS)
        (pkg / "models.py").write_text(MODELS)
        (pkg / "migrations" / "__init__.py").write_text("")
        (pkg / "migrations" / "0001_initial.py").write_text(MIGRATION)
        dist = base / "notesapp-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: notesapp\nVersion: 1.0\n")
        (dist / "entry_points.txt").write_text(
            "[blackdiamondhub.plugins]\nnotes = notesapp.plugin:PLUGIN\n")
        self.src, self.dst = base / "src", base / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def run_py(self, data_dir, *args):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("BDH_") and k != "DJANGO_SETTINGS_MODULE"}
        env.update(BDH_APP_ID="notes", BDH_DATA_DIR=str(data_dir),
                   PYTHONPATH=os.pathsep.join([str(self.base), str(ROOT)]))
        proc = subprocess.run([sys.executable, *args], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_round_trip(self):
        self.run_py(self.src, "-c", textwrap.dedent(SEED))
        out = self.base / "notes.json"
        self.run_py(self.src, "-m", "bdh_sdk.runtime.manage", "export_app_data", "-o", str(out))
        fixture = json.loads(out.read_text())
        self.assertEqual([r["model"] for r in fixture], ["notesapp.note", "notesapp.note"])

        # A fresh container: the CLI migrates first, then loads the rows.
        proc = self.run_py(self.dst, "-m", "bdh_sdk.appdata", "import", str(out))
        self.assertIn("imported 2 rows into notesapp", proc.stderr)
        line = next(l for l in self.run_py(self.dst, "-c", textwrap.dedent(CHECK))
                    .stdout.splitlines() if l.startswith("RESULT"))
        result = json.loads(line[len("RESULT"):])
        self.assertEqual(result["rows"][:2], [[1, "hello", "00ff706e67"], [2, "world", None]])
        self.assertEqual(result["new_pk"], 3)

        # --replace swaps the rows instead of failing on duplicate keys.
        self.run_py(self.dst, "-m", "bdh_sdk.runtime.manage", "import_app_data", str(out),
                    "--replace")
        line = next(l for l in self.run_py(self.dst, "-c", textwrap.dedent(CHECK))
                    .stdout.splitlines() if l.startswith("RESULT"))
        self.assertEqual([r[1] for r in json.loads(line[len("RESULT"):])["rows"]],
                         ["hello", "world", "after"])

    def test_refuses_other_apps_rows(self):
        self.run_py(self.src, "-c", textwrap.dedent(SEED))
        bad = self.base / "bad.json"
        bad.write_text(json.dumps([{"model": "contenttypes.contenttype", "pk": 99, "fields": {"app_label": "x", "model": "y"}}]))
        env_proc = subprocess.run(
            [sys.executable, "-m", "bdh_sdk.appdata", "import", str(bad)],
            env={**{k: v for k, v in os.environ.items() if not k.startswith("BDH_")
                    and k != "DJANGO_SETTINGS_MODULE"},
                 "BDH_APP_ID": "notes", "BDH_DATA_DIR": str(self.src),
                 "PYTHONPATH": os.pathsep.join([str(self.base), str(ROOT)])},
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(env_proc.returncode, 0)
        self.assertIn("row of app 'contenttypes'", env_proc.stderr)


if __name__ == "__main__":
    unittest.main()
