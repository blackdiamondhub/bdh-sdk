"""Test helpers for hub apps.

Nothing here imports Django at module import time.
"""
import json
import os
import subprocess
import sys

__all__ = ["FakeBroker", "PluginImportError", "assert_plugin_importable", "probe_plugin_import"]

DEFAULT_FORBIDDEN = ("requests",)

_PROBE = r"""
import json, sys, threading

opts = json.loads(sys.argv[1])
module, app, attr = opts["module"], opts["app"], opts["attr"]
before_threads = {t.ident for t in threading.enumerate()}
before_modules = set(sys.modules)

import importlib
spec = getattr(importlib.import_module(module), attr, None)

new_modules = set(sys.modules) - before_modules
names = opts["forbidden"]
forbidden = sorted(
    m for m in new_modules
    if any(m == n or m.startswith(n + ".") for n in names)
    or m in {app + ".models", app + ".views", app + ".homepage"}
    or m.endswith(".models")
)
apps_ready = False
if "django.apps" in sys.modules:
    apps_ready = sys.modules["django.apps"].apps.ready
settings_configured = False
if "django.conf" in sys.modules:
    settings_configured = sys.modules["django.conf"].settings.configured
print(json.dumps({
    "spec_type": type(spec).__name__,
    "forbidden": forbidden,
    "new_threads": [t.name for t in threading.enumerate() if t.ident not in before_threads],
    "apps_ready": apps_ready,
    "settings_configured": settings_configured,
}))
"""


class PluginImportError(AssertionError):
    """Importing a plugin module had side effects or produced no spec."""


def probe_plugin_import(module_path, *, app=None, attr="spec", forbidden=(),
                        path=(), cwd=None, timeout=60):
    """Import ``module_path`` in a fresh interpreter and report what happened.

    Returns a dict with ``spec_type``, ``forbidden`` (newly imported modules
    that a plugin module must not pull in), ``new_threads``, ``apps_ready``
    and ``settings_configured``. The child sees the caller's ``sys.path``
    plus ``path``; ``DJANGO_SETTINGS_MODULE`` is removed from its environment.
    """
    if app is None:
        app = module_path.split(".")[0]
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    search = [os.fspath(p) for p in path] + [p for p in sys.path if p]
    if env.get("PYTHONPATH"):
        search.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(search)
    opts = {
        "module": module_path,
        "app": app,
        "attr": attr,
        "forbidden": [*DEFAULT_FORBIDDEN, *forbidden],
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(opts)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise PluginImportError(
            f"importing {module_path} failed:\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def assert_plugin_importable(module_path, **kwargs):
    """Assert ``module_path`` exposes a PluginSpec and imports cleanly.

    Importing a plugin module must not set up Django or configure settings,
    start threads, or import ``requests`` or the app's ``models``, ``views``
    or ``homepage`` modules (any ``*.models`` module counts). Pass
    ``forbidden=("name", ...)`` to ban more modules (a name also bans its
    submodules). Other keyword arguments go to :func:`probe_plugin_import`.
    Raises :class:`PluginImportError` (an ``AssertionError``) on failure and
    returns the probe result otherwise.
    """
    result = probe_plugin_import(module_path, **kwargs)
    problems = []
    attr = kwargs.get("attr", "spec")
    if result["spec_type"] != "PluginSpec":
        problems.append(f"{attr} is {result['spec_type']}, expected PluginSpec")
    if result["forbidden"]:
        problems.append(f"imported forbidden modules: {result['forbidden']}")
    if result["new_threads"]:
        problems.append(f"started threads: {result['new_threads']}")
    if result["apps_ready"]:
        problems.append("set up Django (apps registry is ready)")
    if result["settings_configured"]:
        problems.append("configured Django settings")
    if problems:
        raise PluginImportError(
            f"{module_path} is not a clean plugin module: " + "; ".join(problems)
        )
    return result

def _require(grant):
    def rule(grants, app_id, p):
        return None if grant in grants else f"needs the {grant!r} permission"
    return rule


def _own_app(grants, app_id, p):
    return None if p.get("app") == app_id else f"{app_id} may not touch {p.get('app')!r}'s data"


def _settings_rule(grants, app_id, p):
    if p.get("app") == "hub" and p.get("key") in ("location", "units"):
        return None
    return _own_app(grants, app_id, p)


def _alerts_rule(grants, app_id, p):
    from . import grants as g
    return _require(g.ALERTS)(grants, app_id, p) or _own_app(grants, app_id, p)


def _publish_rule(grants, app_id, p):
    from . import grants as g
    topic = p.get("topic")
    if not isinstance(topic, str) or topic.startswith("_"):
        return f"may not publish {topic!r}"
    return None if g.can_publish(grants, app_id, topic) else f"may not publish {topic!r}"


def _subscribe_rule(grants, app_id, p):
    from . import grants as g
    patterns = [x for x in p.get("patterns") or () if isinstance(x, str) and not x.startswith("_")]
    refused = [x for x in patterns if not g.can_subscribe(grants, app_id, x)]
    return f"may not subscribe to {', '.join(refused)}" if refused else None


def _read_one(grants, app_id, p):
    from . import grants as g
    eid = p.get("entity_id")
    return None if g.can_read(grants, eid) else f"may not read {eid}"


def _read_many(grants, app_id, p):
    from . import grants as g
    refused = [i for i in p.get("entity_ids") or () if not g.can_read(grants, i)]
    return f"may not read {', '.join(map(str, refused[:5]))}" if refused else None


def _read_any(grants, app_id, p):
    from . import grants as g
    return None if g.can_read_any(grants) else "needs an ha.read permission"


def _call_rule(grants, app_id, p):
    from . import grants as g
    domain, service = p.get("domain"), p.get("service")
    return None if g.can_call(grants, domain, service) else f"may not call {domain}.{service}"


def _anyone(grants, app_id, p):
    return None


# What the hub broker checks before running each op (BlackDiamondHub
# broker/rpc.py). ``rule(grants, app_id, payload)`` returns why the call is
# refused, or None. The hub's tests check this covers every op it serves.
PERMISSION_RULES = {
    "settings.get": _settings_rule,
    "state.get": _own_app,
    "state.set": _own_app,
    "state.delete": _own_app,
    "state.keys": _own_app,
    "secrets.get": _anyone,  # always the caller's own secrets
    "secrets.set": _anyone,
    "secrets.delete": _anyone,
    "alerts.push": _alerts_rule,
    "alerts.dismiss": _alerts_rule,
    "events.publish": _publish_rule,
    "events.subscribe": _subscribe_rule,
    "ha.get_entity_state": _read_one,
    "ha.get_entity_states": _read_many,
    "ha.get_history": _read_many,
    "ha.call_service": _call_rule,
    "ha.upload_media": _require("media"),
    "ha.get_tts": _require("media"),
    "ha.get_all_states": _read_any,
    "ha.get_static": _require("media"),
    "ha.ws_commands": _require("ha.ws"),
    "ha.instances": _read_any,
    "ha.registry": _read_any,
    "speakers.list": _require("speakers"),
    "speakers.announce": _require("speakers"),
    "speakers.tts": _require("speakers"),
    "hub.brand": _anyone,
    "shares.missing": _anyone,  # shares.* enforce their own rules
    "shares.put_blob": _anyone,
    "shares.publish": _anyone,
    "shares.list": _anyone,
    "shares.open": _anyone,
}

DEFAULT_INSTANCE = {"id": "ha", "name": "Home Assistant", "site": "", "default": True}


class FakeBroker:
    """A broker on ``127.0.0.1`` for app tests, in a background thread.

    **Prefer** :meth:`for_plugin` (``FakeBroker.for_plugin(spec, install=True)``):
    it grants the app exactly its manifest ``permissions`` and refuses calls
    the way the hub does (:data:`PERMISSION_RULES`, 403 ``forbidden``), and
    answers HA reads only for entities those permissions cover. A test that
    passes against it won't hit a permission error on a real hub.

    Home Assistant: :attr:`instances` lists the hub's instances (one default
    ``ha`` unless set). :attr:`registries` and :attr:`ha_states` map an
    instance id to its registry (``{"entities", "devices", "areas",
    "unit_system"}``) and its state list; :attr:`registry` is the default
    instance's registry. A payload's ``instance`` picks one (absent means the
    default); an unknown id is ``invalid``.

    It records every call in :attr:`calls` (``(op, payload)``) and answers
    ``settings.*`` and ``state.*`` from :attr:`settings` / :attr:`state`
    (``{(app, key): value}``), and ``secrets.*`` from :attr:`secrets`
    (``{(app, name): value}``) for the broker's own :attr:`app_id` only, the
    way the hub ties secrets to the app's token, and ``ha.registry`` from
    :attr:`registry` (narrowed by the payload's ``platform``). It runs the
    ``shares.*`` ops in memory under the hub's rules, with the broker's
    :attr:`app_id` as the caller (set it to act as another app): declare
    shares with :meth:`set_manifest_shares`, grant them with
    :meth:`grant_share` / :meth:`revoke_share`; notifications land in
    :attr:`share_events` as ``(recipient, topic, payload)``. Anything else answers ``None`` unless a
    handler is registered with :meth:`on` (``fn(payload) -> result``; raise
    :class:`FakeBroker.Error` to fail the call). With ``permissions`` (a
    list of manifest permission strings) every op is checked first under the
    hub's rules; ops the hub doesn't serve get 404. Without it, ops outside
    :attr:`grants` (fnmatch patterns over op names; default everything) get
    403. Use as a context manager;
    ``install=True`` also points the SDK at it via :func:`bdh_sdk.rpc.install`.
    """

    class Error(Exception):
        def __init__(self, code, message="", status=400):
            super().__init__(message or code)
            self.code, self.message, self.status = code, message or code, status

    def __init__(self, token="test-token", *, grants=("*",), permissions=None, install=False,
                 app_id="test"):
        self.token = token
        self.app_id = app_id
        self.grants = list(grants)
        self.permissions = None if permissions is None else list(permissions)
        self.calls = []
        self.settings = {}
        self.state = {}
        self.secrets = {}
        self.instances = [dict(DEFAULT_INSTANCE)]
        self.registries = {}
        self.ha_states = {}
        self.registry = {"entities": [], "devices": [], "areas": [], "unit_system": {}}
        self.share_manifests = {}  # app_id -> [{"name", "type", "title"}]
        self.share_blobs = {}  # (owner, sha256) -> {"mime", "size", "data"}
        self.share_revisions = {}  # (provider, name) -> [{"revision", "main", "attachments", "updated"}]
        self.share_grants = set()  # (consumer, provider, name)
        self.share_events = []  # (recipient app_id, topic, payload)
        self.handlers = {}
        self._install = install
        self._server = None
        self._thread = None
        self.url = ""

    @classmethod
    def for_plugin(cls, spec, **kwargs):
        """A broker that treats callers as ``spec``'s app with its manifest grants."""
        kwargs.setdefault("app_id", spec.id)
        kwargs.setdefault("permissions", tuple(spec.permissions))
        return cls(**kwargs)

    @property
    def registry(self):
        return self.registries.setdefault(self._default_instance(), {})

    @registry.setter
    def registry(self, value):
        self.registries[self._default_instance()] = value

    def _default_instance(self):
        for inst in self.instances:
            if inst.get("default"):
                return inst["id"]
        return self.instances[0]["id"] if self.instances else DEFAULT_INSTANCE["id"]

    def _instance(self, p):
        value = p.get("instance")
        if value is None:
            return self._default_instance()
        if value not in {i["id"] for i in self.instances}:
            if self.permissions is None:  # lenient legacy mode: anything else is the default
                return self._default_instance()
            raise FakeBroker.Error("invalid", f"unknown Home Assistant instance {value!r}")
        return value

    def on(self, op, fn):
        """Answer ``op`` with ``fn(payload)``."""
        self.handlers[op] = fn
        return fn

    def ops(self, op):
        """Payloads of every call to ``op``, in order."""
        return [p for o, p in self.calls if o == op]

    def set_manifest_shares(self, app_id, shares):
        """Declare ``app_id``'s shares, as its ``PluginSpec.shares`` would."""
        self.share_manifests[app_id] = [
            {"name": s["name"], "type": s["type"], "title": s.get("title", s["name"])}
            for s in shares
        ]

    def grant_share(self, consumer, provider, name):
        """Let ``consumer`` open ``provider``'s share ``name``."""
        self.share_grants.add((consumer, provider, name))

    def revoke_share(self, consumer, provider, name):
        self.share_grants.discard((consumer, provider, name))

    def start(self):
        import http.server
        import threading

        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                status, body = broker._handle(self.path, self.headers, self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)))
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-broker",
                                        daemon=True)
        self._thread.start()
        if self._install:
            from . import rpc
            rpc.install(rpc.Client(self.url, self.token, retries=0))
        return self

    def stop(self):
        if self._install:
            from . import rpc
            rpc.flush()
            rpc.uninstall()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _handle(self, path, headers, raw):
        import fnmatch

        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"ok": False, "error": {"code": "unauthorized", "message": "bad token"}}
        if not path.startswith("/rpc/"):
            return 404, {"ok": False, "error": {"code": "not_found", "message": path}}
        op = path[len("/rpc/"):]
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return 400, {"ok": False, "error": {"code": "invalid", "message": "bad JSON"}}
        self.calls.append((op, payload))
        if self.permissions is not None:
            rule = PERMISSION_RULES.get(op)
            if rule is None:
                return 404, {"ok": False, "error": {"code": "not_found", "message": f"no op {op}"}}
            reason = rule(self.permissions, self.app_id, payload if isinstance(payload, dict) else {})
            if reason:
                return 403, {"ok": False, "error": {"code": "forbidden", "message": reason}}
        elif not any(fnmatch.fnmatchcase(op, g) for g in self.grants):
            return 403, {"ok": False, "error": {"code": "forbidden", "message": f"{op} not granted"}}
        try:
            if op in self.handlers:
                result = self.handlers[op](payload)
            else:
                result = self._default(op, payload)
        except FakeBroker.Error as exc:
            return exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        return 200, {"ok": True, "result": result}

    def _default(self, op, p):
        if op.startswith("secrets."):
            return self._secret(op, p)
        if op.startswith("shares."):
            return self._share(op, p)
        key = (p.get("app"), p.get("key"))
        if op == "settings.get":
            return {"found": key in self.settings, "value": self.settings.get(key)}
        if op == "state.get":
            return {"found": key in self.state, "value": self.state.get(key)}
        if op == "state.set":
            self.state[key] = p.get("value")
        elif op == "state.delete":
            self.state.pop(key, None)
        elif op == "state.keys":
            return sorted(k for a, k in self.state if a == p.get("app"))
        elif op == "ha.instances":
            return [dict(i) for i in self.instances]
        elif op == "ha.registry":
            return self._registry(p.get("platform"), self._instance(p))
        elif op == "ha.get_all_states":
            states = self._states(p)
            if states is not None:
                return [s for s in states if self._readable(s.get("entity_id"))]
        elif op == "ha.get_entity_state":
            states = self._states(p)
            if states is not None:
                return next((s for s in states if s.get("entity_id") == p.get("entity_id")), None)
        return None

    def _readable(self, entity_id):
        from . import grants
        return self.permissions is None or grants.can_read(self.permissions, entity_id)

    def _states(self, p):
        """The instance's :attr:`ha_states`, or None when the test set none."""
        return self.ha_states.get(self._instance(p))

    def _registry(self, platform, instance=None):
        from . import grants
        reg = self.registries.get(instance or self._default_instance()) or {}
        if self.permissions is not None:
            return grants.filter_registry(self.permissions, reg, platform)
        return grants.filter_registry(["ha.read:*"], reg, platform)

    def _secret(self, op, p):
        name = p.get("name")
        if not isinstance(name, str) or not name:
            raise FakeBroker.Error("invalid", "name is required")
        key = (self.app_id, name)
        if op == "secrets.get":
            return {"found": key in self.secrets, "value": self.secrets.get(key)}
        if op == "secrets.set":
            value = p.get("value")
            if not isinstance(value, str) or not value:
                raise FakeBroker.Error("invalid", "value must be a non-empty string")
            self.secrets[key] = value
        elif op == "secrets.delete":
            self.secrets.pop(key, None)
        return None

    # Shares: the hub's rules (bdh_sdk.shares), kept in memory. The caller is
    # always this broker's app_id; change it to act as another app.
    SHARE_TYPES = {"text/calendar": 2 * 1024 * 1024, "image/jpeg": None,
                   "image/png": None, "image/webp": None}
    SHARE_MAX_BLOB = 6 * 1024 * 1024
    SHARE_QUOTA = 50 * 1024 * 1024
    SHARE_MAX_ATTACHMENTS = 200
    SHARE_KEEP = 2

    def _share(self, op, p):
        import base64
        import binascii
        import hashlib
        import re
        from datetime import datetime, timezone

        me = self.app_id
        hex_re = re.compile(r"[0-9a-f]{64}")
        if op == "shares.missing":
            hashes = p.get("sha256")
            if (not isinstance(hashes, list) or len(hashes) > 500
                    or not all(isinstance(h, str) and hex_re.fullmatch(h) for h in hashes)):
                raise FakeBroker.Error("invalid", "sha256 must be a list of at most 500 hex digests")
            return {"missing": [h for h in hashes if (me, h) not in self.share_blobs]}
        if op == "shares.put_blob":
            mime, text = p.get("mime"), p.get("data_b64")
            if mime not in self.SHARE_TYPES or not isinstance(text, str):
                raise FakeBroker.Error("invalid", f"unsupported mime {mime!r}")
            try:
                data = base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError):
                raise FakeBroker.Error("invalid", "bad data_b64") from None
            limit = self.SHARE_TYPES[mime] or self.SHARE_MAX_BLOB
            if len(data) > min(limit, self.SHARE_MAX_BLOB):
                raise FakeBroker.Error("too_large", "blob too large")
            if mime == "text/calendar":
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    raise FakeBroker.Error("invalid", "text/calendar must be UTF-8") from None
            sha = hashlib.sha256(data).hexdigest()
            if (me, sha) not in self.share_blobs:
                used = sum(b["size"] for (o, _), b in self.share_blobs.items() if o == me)
                if used + len(data) > self.SHARE_QUOTA:
                    raise FakeBroker.Error("too_large", "share quota exceeded")
                self.share_blobs[(me, sha)] = {"mime": mime, "size": len(data), "data": data}
            return {"sha256": sha}
        if op == "shares.publish":
            name = p.get("name")
            decl = self._share_decl(me, name)
            if decl is None:
                raise FakeBroker.Error("invalid", f"share {name!r} is not declared")
            main = self.share_blobs.get((me, p.get("main")))
            if main is None or main["mime"] != decl["type"]:
                raise FakeBroker.Error("invalid", f"main must be your own {decl['type']} blob")
            atts = p.get("attachments") or {}
            if not isinstance(atts, dict) or len(atts) > self.SHARE_MAX_ATTACHMENTS:
                raise FakeBroker.Error("invalid", "too many attachments")
            key_re = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
            for key, sha in atts.items():
                if not isinstance(key, str) or not key_re.fullmatch(key):
                    raise FakeBroker.Error("invalid", f"bad attachment key {key!r}")
                blob = self.share_blobs.get((me, sha))
                if blob is None or not blob["mime"].startswith("image/"):
                    raise FakeBroker.Error("invalid", f"attachment {key!r} must be your own image blob")
            revs = self.share_revisions.setdefault((me, name), [])
            current = revs[-1]["revision"] if revs else 0
            expected = p.get("expected_revision")
            if expected is not None and expected != current:
                raise FakeBroker.Error("invalid", f"conflict: revision is {current}, not {expected}")
            revs.append({"revision": current + 1, "main": p["main"], "attachments": dict(atts),
                         "updated": datetime.now(timezone.utc).isoformat()})
            del revs[:-self.SHARE_KEEP]
            payload = {"provider": me, "name": name, "revision": current + 1}
            recipients = [me] + sorted(c for c, pr, n in self.share_grants
                                       if pr == me and n == name and c != me)
            for app in recipients:
                self.share_events.append((app, f"_shares/{me}/{name}", payload))
            return {"revision": current + 1}
        if op == "shares.list":
            out = []
            for provider, decls in sorted(self.share_manifests.items()):
                for decl in decls:
                    if provider != me and (me, provider, decl["name"]) not in self.share_grants:
                        continue
                    revs = self.share_revisions.get((provider, decl["name"])) or []
                    out.append({"provider": provider, "name": decl["name"], "title": decl["title"],
                                "type": decl["type"],
                                "revision": revs[-1]["revision"] if revs else 0,
                                "updated": revs[-1]["updated"] if revs else None})
            return {"shares": out}
        if op == "shares.open":
            provider, name = p.get("provider"), p.get("name")
            if provider != me and (me, provider, name) not in self.share_grants:
                raise FakeBroker.Error("forbidden", "share not granted", status=403)
            decl = self._share_decl(provider, name)
            revs = self.share_revisions.get((provider, name)) or []
            if decl is None or not revs:
                raise FakeBroker.Error("not_found", "no such share", status=404)
            rev = revs[-1]
            if p.get("if_revision") == rev["revision"]:
                return {"revision": rev["revision"], "not_modified": True}
            main = self.share_blobs[(provider, rev["main"])]["data"]
            return {"revision": rev["revision"], "type": decl["type"], "title": decl["title"],
                    "data_b64": base64.b64encode(main).decode("ascii"),
                    "attachments": {k: f"/apps/_share/{sha}" for k, sha in rev["attachments"].items()}}
        raise FakeBroker.Error("unknown_op", op, status=404)

    def _share_decl(self, app_id, name):
        for decl in self.share_manifests.get(app_id, ()):
            if decl["name"] == name:
                return decl
        return None
