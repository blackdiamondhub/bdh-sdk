import tempfile
import unittest
from pathlib import Path

from bdh_sdk import lint


class NativeDialogTests(unittest.TestCase):
    def check(self, files):
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                p = Path(d, name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            return [(rel, n, name) for rel, n, name, _ in lint.native_dialogs(d)]

    def test_finds_the_ways_apps_have_called_them(self):
        found = self.check({
            "app/templates/app/settings.html":
                '<button onclick="return confirm(\'Remove this feed?\')">Remove</button>\n',
            "app/static/app/app.js":
                'if (window.confirm("Delete?")) go();\n'
                "x.catch(function (e) { alert(e.message); });\n"
                "var n = prompt ('Name');\n",
        })
        self.assertEqual(found, [
            ("app/static/app/app.js", 1, "confirm"),
            ("app/static/app/app.js", 2, "alert"),
            ("app/static/app/app.js", 3, "prompt"),
            ("app/templates/app/settings.html", 1, "confirm"),
        ])

    def test_allows_the_replacements_comments_and_lookalikes(self):
        self.assertEqual(self.check({
            "app/static/app/app.js":
                "bdh.confirm('Delete?').then(go);\n"
                "F.confirm(x); confirmRemove(id); showAlert(1); myprompt(2);\n"
                "// a native confirm() would be ignored here\n"
                "/* alert(1)\n   prompt(2) */\n"
                "var u = 'https://x.y/'; go(); // confirm(\n"
                "legacy(confirm('ok')); // bdh-lint: allow-dialog\n",
            "app/templates/app/a.html":
                '<button data-bdh-confirm="Remove?">x</button><!-- confirm("old") -->\n',
        }), [])

    def test_skips_tests_vendored_and_minified_files(self):
        self.assertEqual(self.check({
            "app/tests/test_ui.js": "alert(1);\n",
            "app/static/vendor/lib.js": "alert(1);\n",
            "app/static/app/chart.min.js": "alert(1);\n",
            "app/notes.txt": "alert(1);\n",
        }), [])

    def test_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.js").write_text("alert(1);\n")
            self.assertEqual(lint.main([d]), 1)
            Path(d, "a.js").write_text("bdh.alert(1);\n")
            self.assertEqual(lint.main([d]), 0)


if __name__ == "__main__":
    unittest.main()
