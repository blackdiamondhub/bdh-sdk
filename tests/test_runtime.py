"""bdh_sdk.runtime serving a tiny app, in a subprocess (Django can only be set up once)."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEMO_INIT = '''
def status():
    return "all good"
'''
DEMO_URLS = '''
from django.http import JsonResponse
from django.urls import path


def whoami(request):
    return JsonResponse({"user": request.user.username, "staff": request.user.is_staff,
                         "mode": request.bdh_mode, "cookie": request.COOKIES.get("x", "")})


urlpatterns = [path("", whoami, name="demo_home")]
'''
SCRIPT = '''
import json, os
from bdh_sdk import identity, plugins, runtime

spec = plugins.PluginSpec(
    id="demo", app="demoapp", urls="demoapp.urls", status="demoapp.status",
    tiles=(plugins.Tile(title="Demo", url_name="demo_home", icon="demo/icon.svg", section="more",
                              icon_on="demo/icon-on.svg"),),
    settings=(plugins.Setting(key="color", label="Colour", type="choice", choices=("red", "blue")),),
    permissions=("alerts",),
)
runtime.app_spec = lambda: spec
runtime.setup()

from django.test import Client

TOKEN = os.environ["BDH_APP_TOKEN"]
c = Client()
out = {}


def get(path, ident=None, **headers):
    h = identity.signed_headers(TOKEN, "GET", path, ident)
    h.update(headers)
    return c.get(path, headers=h)


def post(path, body, sign_body=True):
    raw = json.dumps(body).encode()
    h = identity.signed_headers(TOKEN, "POST", path, body=raw if sign_body else None)
    return c.post(path, raw, content_type="application/json", headers=h)


out["health"] = c.get("/_bdh/health").json()
out["unsigned"] = c.get("/demo/").status_code
out["visitor"] = get("/demo/", {"u": "sam", "staff": True, "mode": "owner"}, Cookie="x=1").json()
anon = get("/demo/")
out["anon"] = anon.json()
out["referrer_policy"] = anon.headers.get("Referrer-Policy")
out["manifest"] = c.get("/_bdh/manifest", headers=identity.signed_headers(TOKEN, "GET", "/_bdh/manifest", body=b"")).json()
out["manifest_unsigned"] = c.get("/_bdh/manifest").status_code
out["hook"] = post("/_bdh/hook", {"hook": "status"}).json()
out["hook_unsigned_body"] = post("/_bdh/hook", {"hook": "status"}, sign_body=False).status_code
out["hook_missing"] = post("/_bdh/hook", {"hook": "card"}).status_code
out["hook_bad"] = post("/_bdh/hook", {"nope": 1}).status_code
from bdh_sdk import events
got = []
events.subscribe("demo/+", lambda e: got.append(e.topic))
out["event"] = post("/_bdh/events", {"topic": "demo/ping", "data": {}}).json()
events.flush()
out["got"] = got
r = c.get("/demo/", headers=identity.signed_headers(TOKEN, "GET", "/_bdh/hook"))
out["wrong_path_sig"] = r.status_code
from bdh_sdk import is_admin, push


def internal_get(path, ident=None):
    return c.get(path, headers=identity.signed_headers(TOKEN, "GET", path, ident, body=b""))


out["push_off"] = internal_get("/_bdh/push/authorize").status_code
push.authorize(lambda request: ["demo/#", "other/#", "demo/x/+"] if is_admin(request) else [])
admin = {"u": "sam", "staff": True, "mode": "owner"}
out["push_admin"] = internal_get("/_bdh/push/authorize", admin).json()
out["push_anon"] = internal_get("/_bdh/push/authorize").json()
out["push_unsigned"] = c.get("/_bdh/push/authorize").status_code
# Signed like a page request (no body hash): a visitor can't ask for themselves.
out["push_proxied"] = get("/_bdh/push/authorize", admin).status_code
print("RESULT" + json.dumps(out))
'''


@unittest.skipIf(find_spec("django") is None or find_spec("whitenoise") is None,
                 "runtime extra not installed")
class RuntimeTests(unittest.TestCase):
    def test_serves_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp, "demoapp")
            pkg.mkdir()
            (pkg / "__init__.py").write_text(DEMO_INIT)
            (pkg / "urls.py").write_text(DEMO_URLS)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("BDH_") and k != "DJANGO_SETTINGS_MODULE"}
            env.update(BDH_APP_ID="demo", BDH_APP_TOKEN="tok", BDH_DATA_DIR=tmp,
                       PYTHONPATH=os.pathsep.join([tmp, str(ROOT)]))
            proc = subprocess.run([sys.executable, "-c", textwrap.dedent(SCRIPT)], env=env,
                                  capture_output=True, text=True, timeout=60)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
        self.assertIsNotNone(line, proc.stderr)
        out = json.loads(line[len("RESULT"):])
        self.assertEqual(out["health"], {"ok": True, "app": "demo"})
        self.assertEqual(out["unsigned"], 403)
        self.assertEqual(out["visitor"], {"user": "sam", "staff": True, "mode": "owner", "cookie": "1"})
        self.assertEqual(out["anon"], {"user": "", "staff": False, "mode": "guest", "cookie": ""})
        # Map tile servers (OSM) block requests with no Referer, so cross-origin
        # requests must still carry the app's origin.
        self.assertEqual(out["referrer_policy"], "strict-origin-when-cross-origin")
        m = out["manifest"]
        self.assertEqual(m["id"], "demo")
        self.assertEqual(m["permissions"], ["alerts"])
        self.assertEqual(m["hooks"], ["status"])
        self.assertEqual(m["tiles"][0]["url"], "/demo/")
        self.assertEqual(m["tiles"][0]["icon_on"], "demo/icon-on.svg")
        self.assertEqual(m["settings"][0]["choices"], [["red", "red"], ["blue", "blue"]])
        self.assertEqual(out["manifest_unsigned"], 403)
        self.assertEqual(out["hook"], {"result": {"text": "all good", "level": "info"}})
        self.assertEqual(out["hook_unsigned_body"], 403)
        self.assertEqual(out["hook_missing"], 404)
        self.assertEqual(out["hook_bad"], 400)
        self.assertEqual(out["event"], {"ok": True})
        self.assertEqual(out["got"], ["demo/ping"])
        self.assertEqual(out["wrong_path_sig"], 403)
        self.assertEqual(out["push_off"], 404)
        self.assertEqual(out["push_admin"], {"patterns": ["demo/#", "demo/x/+"]})
        self.assertEqual(out["push_anon"], {"patterns": []})
        self.assertEqual(out["push_unsigned"], 403)
        self.assertEqual(out["push_proxied"], 403)


if __name__ == "__main__":
    unittest.main()


class RuntimeTemplateLayoutTests(unittest.TestCase):
    """Runtime pages render inside the hub's iframe, already below the hub
    navbar, so they must not reserve space for it (top:62px, 100vh-62px,
    phone 118px) or scope rules under the hub's ``.navbar``."""

    def test_no_hub_navbar_offsets(self):
        import re
        import bdh_sdk.runtime as rt
        root = Path(rt.__file__).resolve().parent / "templates"
        self.assertTrue(root.is_dir())
        pattern = re.compile(r"top\s*:\s*(6[0-9]|118)px|100vh\s*-\s*(6[0-9]|118)px|\.navbar(?![-\w])")
        hits = []
        for path in root.glob("**/*.html"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])
