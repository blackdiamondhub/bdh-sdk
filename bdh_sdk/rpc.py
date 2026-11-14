"""Talks to the hub broker when an app runs in its own container.

The hub starts each app container with ``BDH_BROKER_URL``, ``BDH_APP_ID`` and
``BDH_APP_TOKEN``. :func:`install` then points the SDK's settings, state,
alerts, events, Home Assistant and speaker modules at the broker, so app code
calls the same functions it always has.

Every call is ``POST {BDH_BROKER_URL}/rpc/<op>`` with a JSON body and the
app's bearer token. The broker answers ``{"ok": true, "result": ...}`` or
``{"ok": false, "error": {"code": ..., "message": ...}}``. Binary data travels
base64-encoded. The broker checks each op against the app's grants.

Alerts and events are fire-and-forget: they go out on one background thread
so the caller never blocks.
"""
import base64
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

__all__ = [
    "APP_ENV",
    "BROKER_ENV",
    "TOKEN_ENV",
    "BrokerError",
    "Client",
    "PermissionDenied",
    "client",
    "configured",
    "flush",
    "install",
    "installed",
    "uninstall",
]

log = logging.getLogger(__name__)

BROKER_ENV = "BDH_BROKER_URL"
TOKEN_ENV = "BDH_APP_TOKEN"
APP_ENV = "BDH_APP_ID"

# Ops that change nothing and are safe to send twice.
_READ_OPS = frozenset({
    "settings.get", "state.get", "state.keys", "hub.brand",
    "ha.get_entity_state", "ha.get_entity_states", "ha.get_history",
    "ha.get_all_states", "ha.get_static", "speakers.list",
})
_RETRY_STATUS = frozenset({502, 503, 504})


class BrokerError(Exception):
    """The broker refused or failed a call."""

    def __init__(self, message, code="error", status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class PermissionDenied(BrokerError):
    """The app wasn't granted what the call needs."""


class Client:
    """A minimal JSON-over-HTTP client for the broker."""

    def __init__(self, url, token, *, timeout=10.0, retries=2, backoff=0.25):
        if not url:
            raise ValueError("broker url is required")
        self.url = url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def call(self, op, payload=None, *, timeout=None):
        """Run ``op`` and return its result; raises :class:`BrokerError`."""
        body = json.dumps(payload or {}).encode()
        attempts = 1 + (self.retries if op in _READ_OPS else 0)
        for attempt in range(attempts):
            try:
                return self._send(op, body, timeout or self.timeout)
            except _Retry as exc:
                if attempt + 1 >= attempts:
                    raise BrokerError(str(exc), "unavailable", exc.status) from None
                time.sleep(self.backoff * (2 ** attempt))

    def _send(self, op, body, timeout):
        req = urllib.request.Request(
            f"{self.url}/rpc/{op}", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise _Retry(f"broker unreachable: {exc}", None) from None
        if status in _RETRY_STATUS:
            raise _Retry(f"broker returned {status}", status)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise BrokerError(f"broker returned {status} with a non-JSON body",
                              "bad_response", status) from None
        if status == 200 and data.get("ok"):
            return data.get("result")
        error = data.get("error") or {}
        code = error.get("code") or "error"
        message = error.get("message") or f"broker returned {status}"
        if status == 403 or code == "forbidden":
            raise PermissionDenied(message, code, status)
        raise BrokerError(message, code, status)


class _Retry(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _unb64(text):
    return base64.b64decode(text.encode("ascii"))


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


class _Background:
    """Sends fire-and-forget calls in order on one daemon thread."""

    def __init__(self, client):
        self.client = client
        self.queue = queue.Queue(maxsize=1000)
        self.thread = None
        self.lock = threading.Lock()

    def send(self, op, payload):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="bdh-rpc", daemon=True)
                self.thread.start()
        try:
            self.queue.put_nowait((op, payload))
        except queue.Full:
            log.warning("broker queue full; dropping %s", op)

    def join(self, timeout=5.0):
        """Wait until everything queued has been sent. Meant for tests."""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def _run(self):
        while True:
            op, payload = self.queue.get()
            try:
                self.client.call(op, payload)
            except Exception as exc:
                log.warning("broker %s failed: %s", op, exc)
            finally:
                self.queue.task_done()


class SettingsLookup:
    """``settings.set_store`` lookup; caches answers for ``ttl`` seconds."""

    def __init__(self, client, ttl=5.0):
        self.client = client
        self.ttl = ttl
        self._cache = {}
        self._lock = threading.Lock()

    def __call__(self, app_id, key):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get((app_id, key))
        if hit and now - hit[0] < self.ttl:
            found, value = hit[1]
        else:
            try:
                res = self.client.call("settings.get", {"app": app_id, "key": key})
            except PermissionDenied:
                raise
            except BrokerError:
                if hit is None:
                    raise
                log.warning("broker unavailable; using cached setting %s.%s", app_id, key)
                return self._stale(hit, key)
            found, value = bool(res.get("found")), res.get("value")
            with self._lock:
                self._cache[(app_id, key)] = (now, (found, value))
        if not found:
            raise KeyError(key)
        return value

    @staticmethod
    def _stale(hit, key):
        found, value = hit[1]
        if not found:
            raise KeyError(key)
        return value

    def clear(self):
        with self._lock:
            self._cache.clear()


class StateBackend:
    """``storage.set_state_backend`` backend backed by the broker."""

    def __init__(self, client):
        self.client = client

    def get(self, app_id, key):
        res = self.client.call("state.get", {"app": app_id, "key": key})
        if not res.get("found"):
            raise KeyError(key)
        return res.get("value")

    def set(self, app_id, key, value):
        self.client.call("state.set", {"app": app_id, "key": key, "value": value})

    def delete(self, app_id, key):
        self.client.call("state.delete", {"app": app_id, "key": key})

    def keys(self, app_id):
        return list(self.client.call("state.keys", {"app": app_id}) or [])


class HABackend:
    """``ha.set_backend`` backend: Home Assistant calls go via the broker,
    which holds the token and checks the app's ``ha.*`` grants."""

    def __init__(self, client):
        self.client = client

    def _call(self, op, payload=None, *, timeout=None):
        from . import ha
        try:
            return self.client.call(f"ha.{op}", payload, timeout=timeout)
        except PermissionDenied:
            raise
        except BrokerError as exc:
            if exc.code == "ha_error":
                raise ha.HAError(str(exc)) from None
            raise

    def get_entity_state(self, entity_id):
        return self._call("get_entity_state", {"entity_id": entity_id})

    def get_entity_states(self, entity_ids):
        return self._call("get_entity_states", {"entity_ids": list(entity_ids)}) or {}

    def get_history(self, entity_ids, start, end=None):
        res = self._call("get_history", {"entity_ids": list(entity_ids),
                                         "start": _iso(start), "end": _iso(end)}) or {}
        return {eid: [tuple(p) for p in points] for eid, points in res.items()}

    def call_service(self, domain, service, entity_id, extra_data=None):
        ok, err = self._call("call_service", {"domain": domain, "service": service,
                                              "entity_id": entity_id, "extra_data": extra_data})
        return ok, err

    def upload_media(self, folder, filename, data, content_type):
        return self._call("upload_media", {"folder": folder, "filename": filename,
                                           "data": _b64(data), "content_type": content_type},
                          timeout=60)

    def get_tts(self, engine_id, message, options=None):
        res = self._call("get_tts", {"engine_id": engine_id, "message": message,
                                     "options": options}, timeout=60)
        return _unb64(res["audio"]), res["content_type"]

    def get_all_states(self):
        return self._call("get_all_states") or []

    def get_static(self, path):
        return _unb64(self._call("get_static", {"path": path}))

    def ws_commands(self, *messages):
        return self._call("ws_commands", {"messages": list(messages)}, timeout=30)


class SpeakersBackend:
    """``speakers.set_backend`` backend; the broker checks the ``speakers``
    grant and does the HA work itself."""

    def __init__(self, client):
        self.client = client

    def _call(self, op, payload=None, *, timeout=None):
        from . import speakers
        try:
            return self.client.call(f"speakers.{op}", payload, timeout=timeout)
        except PermissionDenied:
            raise
        except BrokerError as exc:
            if exc.code == "announce_error":
                raise speakers.AnnounceError(str(exc)) from None
            if exc.code == "invalid":
                raise ValueError(str(exc)) from None
            raise

    def list_speakers(self):
        return [tuple(pair) for pair in self._call("list") or []]

    def announce(self, message, speakers, volume=None, chime=None, tts_engine=None, voice=None):
        payload = {"message": message, "speakers": list(speakers), "volume": volume,
                   "chime": chime, "voice": voice}
        if tts_engine is not None:
            payload["tts_engine"] = tts_engine
        return self._call("announce", payload, timeout=120) or []

    def tts_audio(self, message, tts_engine=None, voice=None):
        payload = {"message": message, "voice": voice}
        if tts_engine is not None:
            payload["tts_engine"] = tts_engine
        res = self._call("tts", payload, timeout=60)
        return _unb64(res["audio"]), res["content_type"]


_client = None
_background = None
_unsubscribe = None
_lock = threading.Lock()


def configured():
    """True when the environment says this app runs behind a broker."""
    return bool(os.environ.get(BROKER_ENV))


def installed():
    return _client is not None


def client():
    """The installed :class:`Client`; raises if :func:`install` hasn't run."""
    if _client is None:
        raise BrokerError("bdh_sdk.rpc is not installed", "not_installed")
    return _client


def install(rpc_client=None):
    """Point the SDK at the broker. Uses the environment unless a client is given."""
    global _client, _background, _unsubscribe
    from . import alerts, events, ha, settings, speakers, storage

    if rpc_client is None:
        rpc_client = Client(os.environ[BROKER_ENV], os.environ.get(TOKEN_ENV, ""))
    uninstall()
    with _lock:
        _client = rpc_client
        _background = _Background(rpc_client)
        bg = _background

    settings.set_store(SettingsLookup(rpc_client))
    storage.set_state_backend(StateBackend(rpc_client))
    alerts.set_sender(lambda alert: bg.send("alerts.push", alert.as_dict()),
                      lambda app, key: bg.send("alerts.dismiss", {"app": app, "key": key}))
    ha.set_backend(HABackend(rpc_client))
    speakers.set_backend(SpeakersBackend(rpc_client))
    events.set_forwarder(lambda ev: bg.send("events.publish", {
        "topic": ev.topic, "data": ev.data, "time": ev.time}))
    _unsubscribe = events.add_listener(
        lambda patterns: bg.send("events.subscribe", {"patterns": patterns}))
    return rpc_client


def uninstall():
    """Undo :func:`install`; the SDK goes back to its in-process defaults."""
    global _client, _background, _unsubscribe
    from . import alerts, events, ha, settings, speakers, storage

    with _lock:
        was = _client is not None
        unsub, _unsubscribe = _unsubscribe, None
        _client = None
        _background = None
    if unsub:
        unsub()
    if not was:
        return
    settings.set_store(None)
    storage.set_state_backend(None)
    alerts.set_sender(None)
    ha.set_backend(None)
    speakers.set_backend(None)
    events.set_forwarder(None)


def flush(timeout=5.0):
    """Wait until queued alerts and events have been sent. Meant for tests."""
    bg = _background
    return True if bg is None else bg.join(timeout)
