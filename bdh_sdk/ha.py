"""Home Assistant client for hub apps.

Fetches entity states and history and calls services over the REST API, and
runs registry commands over the WebSocket API.

A hub can talk to several Home Assistant instances (one per site). Every call
takes an optional ``instance`` id; ``None`` means the default instance.
:func:`instances` lists them. The hub supplies the instances with
:func:`set_provider`; without one there is a single instance, ``ha``, whose
connection comes from ``HOMEASSISTANT_URL`` and ``HOMEASSISTANT_ACCESS_TOKEN``
(Django settings when configured, otherwise the environment).

Needs ``requests`` (and ``websocket-client`` for :func:`ws_commands`):
install ``bdh-sdk[ha]``. The hub already ships both.
"""
import json
import logging
import os
from urllib.parse import urljoin

import requests

from .settings import _django_settings

__all__ = [
    "DEFAULT_INSTANCE",
    "HAError",
    "TIMEOUT",
    "call_service",
    "get_all_states",
    "get_entity_state",
    "get_entity_states",
    "get_history",
    "get_static",
    "get_tts",
    "instances",
    "set_backend",
    "set_provider",
    "upload_media",
    "ws_commands",
]

logger = logging.getLogger(__name__)

TIMEOUT = 10  # seconds

DEFAULT_INSTANCE = "ha"


class HAError(Exception):
    """Raised when a Home Assistant call that must succeed fails."""


_backend = None


def set_backend(backend):
    """Route every call through ``backend`` (an object with this module's
    public functions as methods) instead of calling Home Assistant directly.

    Apps running in their own container use the hub broker this way and
    never see the hub's token. ``None`` restores direct calls.
    """
    global _backend
    _backend = backend


def _setting(name, default=None):
    settings = _django_settings()
    value = getattr(settings, name, None) if settings is not None else None
    if value in (None, ""):
        value = os.environ.get(name, default)
    return value


_provider = None


def set_provider(provider):
    """Use ``provider`` for the list of instances and their connections.

    ``provider`` has ``instances()`` (a list of dicts with ``id``, ``name``,
    ``site`` and ``default``) and ``connection(instance)`` (``(url, token)``
    for an instance id, ``None`` meaning the default; raises :class:`HAError`
    for an unknown id). ``None`` restores the single environment instance.
    """
    global _provider
    _provider = provider


class _EnvProvider:
    def instances(self):
        return [{"id": DEFAULT_INSTANCE, "name": "Home Assistant", "site": "", "default": True}]

    def connection(self, instance):
        if instance not in (None, DEFAULT_INSTANCE):
            raise HAError(f"unknown Home Assistant instance {instance!r}")
        return (_setting("HOMEASSISTANT_URL", "http://homeassistant.local:8123"),
                _setting("HOMEASSISTANT_ACCESS_TOKEN", ""))


def _conn(instance):
    return (_provider or _EnvProvider()).connection(instance)


def _base_url(instance=None):
    return _conn(instance)[0]


def _headers(token):
    return {
        "Authorization": "Bearer " + (token or ""),
        "Content-Type": "application/json",
    }


def _kw(instance):
    return {} if instance is None else {"instance": instance}


def instances():
    """The Home Assistant instances: dicts with ``id``, ``name``, ``site`` and
    ``default`` (exactly one is the default)."""
    if _backend is not None:
        return _backend.instances()
    return (_provider or _EnvProvider()).instances()


def get_entity_state(entity_id, instance=None):
    """State dict (``entity_id``, ``state``, ``attributes``) or None on failure."""
    if _backend is not None:
        return _backend.get_entity_state(entity_id, **_kw(instance))
    try:
        base, token = _conn(instance)
    except HAError as exc:
        logger.error("%s", exc)
        return None
    url = urljoin(base, f"/api/states/{entity_id}")
    try:
        resp = requests.get(url, headers=_headers(token), timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("HA returned %s for %s", resp.status_code, entity_id)
    except requests.RequestException as exc:
        logger.error("Failed to get state for %s: %s", entity_id, exc)
    return None


def get_entity_states(entity_ids, instance=None):
    """Dict of entity_id -> state dict for the wanted entities ({} on failure)."""
    if _backend is not None:
        return _backend.get_entity_states(entity_ids, **_kw(instance))
    try:
        base, token = _conn(instance)
    except HAError as exc:
        logger.error("%s", exc)
        return {}
    url = urljoin(base, "/api/states")
    try:
        resp = requests.get(url, headers=_headers(token), timeout=TIMEOUT)
        if resp.status_code == 200:
            wanted = set(entity_ids)
            return {s["entity_id"]: s for s in resp.json() if s["entity_id"] in wanted}
        logger.warning("HA returned %s fetching all states", resp.status_code)
    except requests.RequestException as exc:
        logger.error("Failed to fetch states: %s", exc)
    return {}


def get_history(entity_ids, start, end=None, instance=None):
    """State history between ``start`` and ``end`` (timezone-aware datetimes).

    Returns entity_id -> list of (iso_timestamp, state), oldest first, or {}
    on failure.
    """
    if _backend is not None:
        return _backend.get_history(entity_ids, start, end, **_kw(instance))
    try:
        base, token = _conn(instance)
    except HAError as exc:
        logger.error("%s", exc)
        return {}
    url = urljoin(base, f"/api/history/period/{start.isoformat()}")
    params = {
        "filter_entity_id": ",".join(entity_ids),
        "minimal_response": "",
        "no_attributes": "",
    }
    if end is not None:
        params["end_time"] = end.isoformat()
    try:
        resp = requests.get(url, headers=_headers(token), params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.warning("HA returned %s fetching history", resp.status_code)
            return {}
        result = {}
        for series in resp.json():
            if not series:
                continue
            # With minimal_response only the first item carries entity_id.
            eid = series[0].get("entity_id")
            result[eid] = [
                (item.get("last_changed") or item.get("last_updated"), item.get("state"))
                for item in series
            ]
        return result
    except (requests.RequestException, ValueError) as exc:
        logger.error("Failed to fetch history: %s", exc)
    return {}


def call_service(domain, service, entity_id, extra_data=None, instance=None):
    """Call ``domain.service`` on ``entity_id``. Returns (success, error or None).

    Pass ``entity_id=None`` for services that take no entity (e.g. ``notify``).
    """
    if _backend is not None:
        return _backend.call_service(domain, service, entity_id, extra_data, **_kw(instance))
    try:
        base, token = _conn(instance)
    except HAError as exc:
        return False, str(exc)
    url = urljoin(base, f"/api/services/{domain}/{service}")
    payload = {} if entity_id is None else {"entity_id": entity_id}
    if extra_data:
        payload.update(extra_data)
    try:
        resp = requests.post(url, headers=_headers(token), json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            return True, None
        msg = f"HA returned {resp.status_code}: {resp.text[:200]}"
        logger.warning(msg)
        return False, msg
    except requests.RequestException as exc:
        msg = f"Request failed: {exc}"
        logger.error(msg)
        return False, msg


def upload_media(folder, filename, data, content_type, instance=None):
    """Upload ``data`` to HA's local media under ``folder`` (created if missing).

    Returns the file's ``media-source://`` id. Needs an admin token. Raises
    HAError on failure.
    """
    if _backend is not None:
        return _backend.upload_media(folder, filename, data, content_type, **_kw(instance))
    base, token = _conn(instance)
    url = urljoin(base, "/api/media_source/local_source/upload")
    target = "media-source://media_source/local/" + folder.strip("/")
    try:
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer " + (token or "")},
            data={"media_content_id": target},
            files={"file": (filename, data, content_type)},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HAError(f"upload {filename} failed: {exc}") from exc
    if resp.status_code != 200:
        raise HAError(f"HA returned {resp.status_code} uploading {filename}: {resp.text[:200]}")
    return resp.json()["media_content_id"]


def get_tts(engine_id, message, options=None, instance=None):
    """Speak ``message`` with the TTS entity ``engine_id``.

    Returns ``(audio bytes, content type)``. Raises HAError on failure.
    """
    if _backend is not None:
        return _backend.get_tts(engine_id, message, options, **_kw(instance))
    base, token = _conn(instance)
    url = urljoin(base, "/api/tts_get_url")
    payload = {"engine_id": engine_id, "message": message}
    if options:
        payload["options"] = options
    try:
        resp = requests.post(url, headers=_headers(token), json=payload, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise HAError(f"HA returned {resp.status_code} for TTS: {resp.text[:200]}")
        # "url" uses HA's external URL; "path" works from the hub's LAN address.
        audio = requests.get(urljoin(base, resp.json()["path"]), headers=_headers(token), timeout=30)
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise HAError(f"TTS failed: {exc}") from exc
    if audio.status_code != 200 or not audio.content:
        raise HAError(f"HA returned {audio.status_code} fetching TTS audio")
    return audio.content, audio.headers.get("Content-Type", "audio/mpeg")


def get_all_states(instance=None):
    """Every entity state of one instance as a list. Raises HAError on failure."""
    if _backend is not None:
        return _backend.get_all_states(**_kw(instance))
    base, token = _conn(instance)
    url = urljoin(base, "/api/states")
    try:
        resp = requests.get(url, headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise HAError(f"states fetch failed: {exc}") from exc
    if resp.status_code != 200:
        raise HAError(f"HA returned {resp.status_code} fetching states")
    return resp.json()


def get_static(path, instance=None):
    """Bytes of a static file served by HA (e.g. an integration's card JS)."""
    if _backend is not None:
        return _backend.get_static(path, **_kw(instance))
    url = urljoin(_base_url(instance), path)
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise HAError(f"fetch {path} failed: {exc}") from exc
    if resp.status_code != 200:
        raise HAError(f"HA returned {resp.status_code} for {path}")
    return resp.content


def ws_commands(*messages, instance=None):
    """Run WebSocket commands (dicts like ``{"type": "config/device_registry/list"}``)
    over one connection and return their results in order.

    Used for data REST doesn't expose (entity / device registries). Raises
    HAError on any failure.
    """
    if _backend is not None:
        return _backend.ws_commands(*messages, **_kw(instance))
    import websocket

    base, token = _conn(instance)
    base = base.rstrip("/")
    url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
    try:
        ws = websocket.create_connection(url, timeout=TIMEOUT)
    except (OSError, websocket.WebSocketException) as exc:
        raise HAError(f"connect failed: {exc}") from exc
    try:
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            raise HAError("auth failed")
        results = []
        for i, message in enumerate(messages, start=1):
            ws.send(json.dumps({**message, "id": i}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == i:
                    break
            if not msg.get("success"):
                raise HAError(f"{message.get('type')} failed: {msg.get('error')}")
            results.append(msg.get("result"))
        return results
    except (OSError, ValueError, websocket.WebSocketException) as exc:
        raise HAError(f"websocket command failed: {exc}") from exc
    finally:
        ws.close()
