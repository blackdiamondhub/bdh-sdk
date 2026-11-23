"""Websockets in bdh_sdk.runtime, in a subprocess (Django can only be set up once)."""
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

DEMO_ROUTING = '''
import json
from django.urls import path
from bdh_sdk import is_admin, wsrelay


async def whoami(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.send", "text": json.dumps({
        "user": scope["user"].username, "admin": is_admin(scope), "mode": scope["bdh_mode"]})})
    await send({"type": "websocket.close", "code": 1000})


async def relay(scope, receive, send):
    await receive()
    port = scope["url_route"]["kwargs"]["port"]
    await wsrelay.relay(receive, send, f"ws://127.0.0.1:{port}/echo",
                        subprotocols=scope.get("subprotocols") or ())


websocket_urlpatterns = [
    path("demo/whoami", whoami),
    path("demo/relay/<int:port>", relay),
]
'''
SCRIPT = '''
import asyncio, json, os
from bdh_sdk import identity, plugins, runtime

spec = plugins.PluginSpec(id="demo", app="demoapp", websocket_routes="demoapp.routing.websocket_urlpatterns")
runtime.app_spec = lambda: spec
from bdh_sdk.runtime.asgi import application
from asgiref.testing import ApplicationCommunicator
from websockets.asyncio.server import serve

TOKEN = os.environ["BDH_APP_TOKEN"]
out = {}


def signed(path, ident=None):
    return [(k.lower().encode(), v.encode()) for k, v in identity.signed_headers(TOKEN, "GET", path, ident).items()]


class WS(ApplicationCommunicator):
    # channels.testing pulls in daphne, which the runtime extra doesn't install.
    def __init__(self, path, headers, subprotocols):
        super().__init__(application, {
            "type": "websocket", "path": path, "raw_path": path.encode(), "query_string": b"",
            "headers": list(headers), "subprotocols": list(subprotocols or ()),
            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 9100)})

    async def connect(self):
        await self.send_input({"type": "websocket.connect"})
        r = await self.receive_output(5)
        if r["type"] == "websocket.accept":
            return True, r.get("subprotocol")
        return False, r.get("code", 1000)

    async def send_to(self, text_data=None, bytes_data=None):
        await self.send_input({"type": "websocket.receive", "text": text_data, "bytes": bytes_data})

    async def receive_from(self):
        return (await self.receive_output(5))["text"]

    async def disconnect(self):
        await self.send_input({"type": "websocket.disconnect", "code": 1000})
        await self.wait(5)


async def connect(path, headers=(), subprotocols=None):
    c = WS(path, headers, subprotocols)
    ok, code = await c.connect()
    return c, ok, code


async def first(path, headers):
    c, ok, code = await connect(path, headers)
    if not ok:
        return code
    msg = json.loads(await c.receive_from())
    await c.disconnect()
    return msg


async def echo(ws):
    async for m in ws:
        await ws.send(m)


async def main():
    out["unsigned"] = await first("/demo/whoami", [])
    out["anon"] = await first("/demo/whoami", signed("/demo/whoami"))
    admin = signed("/demo/whoami", {"u": "sam", "staff": True, "mode": "owner"})
    out["admin"] = await first("/demo/whoami", admin)
    out["replayed"] = await first("/demo/whoami", admin)
    out["other_path"] = await first("/demo/whoami", signed("/demo/other"))
    out["unknown"] = await first("/demo/nope", signed("/demo/nope"))
    out["internal"] = await first("/_bdh/hook", signed("/_bdh/hook"))

    async with serve(echo, "127.0.0.1", 0, subprotocols=["proto"]) as server:
        port = server.sockets[0].getsockname()[1]
        path = f"/demo/relay/{port}"
        c, ok, _ = await connect(path, signed(path), subprotocols=["proto"])
        out["relay_ok"] = ok
        await c.send_to(text_data="hi")
        out["relay_text"] = await c.receive_from()
        await c.send_to(bytes_data=b"\\x00\\x01")
        out["relay_bytes"] = list((await c.receive_output())["bytes"])
        await c.disconnect()
    path = f"/demo/relay/{port}"
    c, ok, _ = await connect(path, signed(path))
    out["relay_down"] = (await c.receive_output()).get("code") if ok else _

asyncio.run(main())
print("RESULT" + json.dumps(out))
'''


@unittest.skipIf(find_spec("django") is None or find_spec("channels") is None
                 or find_spec("websockets") is None, "runtime extra not installed")
class RuntimeWebsocketTests(unittest.TestCase):
    def test_websockets(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp, "demoapp")
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "routing.py").write_text(DEMO_ROUTING)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("BDH_") and k != "DJANGO_SETTINGS_MODULE"}
            env.update(BDH_APP_ID="demo", BDH_APP_TOKEN="tok", BDH_DATA_DIR=tmp,
                       PYTHONPATH=os.pathsep.join([tmp, str(ROOT)]))
            proc = subprocess.run([sys.executable, "-c", textwrap.dedent(SCRIPT)], env=env,
                                  capture_output=True, text=True, timeout=60)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
        self.assertIsNotNone(line, proc.stderr)
        out = json.loads(line[len("RESULT"):])
        self.assertEqual(out["unsigned"], 4403)
        self.assertEqual(out["anon"], {"user": "", "admin": False, "mode": "guest"})
        self.assertEqual(out["admin"], {"user": "sam", "admin": True, "mode": "owner"})
        self.assertEqual(out["replayed"], 4403)
        self.assertEqual(out["other_path"], 4403)
        self.assertEqual(out["unknown"], 4404)
        self.assertEqual(out["internal"], 4403)
        self.assertTrue(out["relay_ok"])
        self.assertEqual(out["relay_text"], "hi")
        self.assertEqual(out["relay_bytes"], [0, 1])
        self.assertEqual(out["relay_down"], 1013)


if __name__ == "__main__":
    unittest.main()
