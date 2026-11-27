"""Checks app CI runs over an app's own files.

``python -m bdh_sdk.lint <app checkout>`` exits 1 and lists each problem.

Native dialogs: the hub shows apps in a sandboxed iframe without
``allow-modals``, so the browser silently ignores ``confirm()``, ``alert()`` and
``prompt()``. ``onclick="return confirm(...)"`` then returns false and the
button does nothing at all. Use ``data-bdh-confirm="..."`` on the button,
``bdh.confirm(message)`` (a Promise) or ``bdh.toast(message, 'error')`` from
bdh/embed.js instead. Add ``bdh-lint: allow-dialog`` to a line to skip it.
"""

import re
import subprocess
import sys
from pathlib import Path

SUFFIXES = {".js", ".mjs", ".html", ".htm"}
SKIP_PARTS = {"tests", "test", "node_modules", "vendor", ".git", "tools"}
ALLOW = "bdh-lint: allow-dialog"
# A bare or window./self./top./parent. call; obj.confirm( (e.g. bdh.confirm) is fine.
DIALOG = re.compile(r"(?:\b(?:window|self|top|parent)\s*\.\s*|(?<![\w.$]))(confirm|alert|prompt)\s*\(")
COMMENT = re.compile(r"/\*.*?\*/|<!--.*?-->|(?<![:'\"\\])//[^\n]*", re.S)


def _files(root):
    root = Path(root)
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        paths = [root / p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for p in root.rglob("*") if p.is_file()]
    for p in sorted(paths):
        rel = p.relative_to(root)
        if p.suffix.lower() in SUFFIXES and not (set(rel.parts[:-1]) & SKIP_PARTS) \
                and not p.name.endswith(".min.js"):
            yield p, rel


def _blank_comments(text):
    """Comments replaced by spaces (newlines kept), so line numbers still match."""
    return COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def native_dialogs(root):
    """[(relative path, line number, dialog name, line text)] for each native dialog call."""
    found = []
    for path, rel in _files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        raw = text.splitlines()
        for n, line in enumerate(_blank_comments(text).splitlines(), 1):
            if ALLOW in raw[n - 1]:
                continue
            for m in DIALOG.finditer(line):
                found.append((str(rel).replace("\\", "/"), n, m.group(1), raw[n - 1].strip()))
    return found


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else "."
    found = native_dialogs(root)
    for rel, n, name, line in found:
        print(f"{rel}:{n}: {name}() is ignored in the hub's sandboxed iframe: {line[:120]}")
    if found:
        print("\nUse data-bdh-confirm=\"...\" on the button, bdh.confirm(message) or bdh.toast(message, 'error') "
              "(bdh/embed.js). See bdh_sdk/lint.py.")
        return 1
    print("No native dialogs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
