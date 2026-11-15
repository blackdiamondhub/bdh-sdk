"""A stand-in hub broker for builder/smoke.sh (stdlib only).

Answers the broker's JSON RPC (``POST /_broker/rpc/<op>``) the way a hub
with nothing configured would: no saved settings (apps fall back to their
environment), empty app state, subscriptions accepted. Anything else is
refused, so an app that needs more of the hub at startup shows it in its log.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {}
OK = {
    "settings.get": lambda p: {"found": False},
    "state.get": lambda p: ({"found": True, "value": STATE[(p.get("app"), p.get("key"))]}
                            if (p.get("app"), p.get("key")) in STATE else {"found": False}),
    "state.set": lambda p: STATE.__setitem__((p.get("app"), p.get("key")), p.get("value")),
    "state.delete": lambda p: STATE.pop((p.get("app"), p.get("key")), None) and None,
    "state.keys": lambda p: [k for a, k in STATE if a == p.get("app")],
    "events.subscribe": lambda p: None,
    "hub.brand": lambda p: {"name": "Smoke"},
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        op = self.path.rstrip("/").rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        if op in OK:
            status, body = 200, {"ok": True, "result": OK[op](payload)}
        else:
            status, body = 404, {"ok": False, "error": {"code": "not_found",
                                                        "message": f"smoke broker: no {op}"}}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        sys.stderr.write("smoke broker: " + (fmt % args) + "\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765),
                        Handler).serve_forever()
