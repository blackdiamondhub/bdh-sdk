"""App shares: files one app publishes and other apps read.

A provider app declares its shares in ``PluginSpec.shares`` and publishes
immutable revisions, each one main file plus image attachments. A consumer
app can open a share once the hub admin has granted it. In the main file,
attachments are referenced as ``bdh-share:<key>``; :func:`resolve` turns such
a reference into a URL the browser can load.

Shares go through the hub broker, so they only work in a container app.
Without a broker every call raises ``SharesError("unavailable")``.
"""
import base64
import binascii
import hashlib
import os
from dataclasses import dataclass, field

from . import rpc

__all__ = [
    "REF_PREFIX",
    "Opened",
    "SharesError",
    "list_shares",
    "open_share",
    "publish",
    "resolve",
]

REF_PREFIX = "bdh-share:"

CODES = frozenset({"forbidden", "not_found", "invalid", "too_large", "unavailable"})

_MISSING_BATCH = 500


class SharesError(Exception):
    """A share call failed; ``code`` is one of ``forbidden``, ``not_found``,
    ``invalid``, ``too_large`` or ``unavailable``."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Opened:
    """One revision of a share, as returned by :func:`open_share`.

    ``attachments`` maps each key to its hub-relative path
    (``/apps/_share/<sha256>``).
    """

    revision: int
    type: str
    title: str
    data: bytes = field(repr=False)
    attachments: dict = field(default_factory=dict, hash=False)
    provider: str = ""
    name: str = ""


def _client():
    if not rpc.installed():
        if not rpc.configured():
            raise SharesError("unavailable", "shares need the hub broker (BDH_BROKER_URL is not set)")
        rpc.install()
    return rpc.client()


def _call(op, payload, *, timeout=None):
    client = _client()
    try:
        return client.call(op, payload, timeout=timeout) or {}
    except rpc.BrokerError as exc:
        code = exc.code if exc.code in CODES else "unavailable"
        raise SharesError(code, str(exc)) from None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def publish(name, data, *, attachments=None, expected_revision=None):
    """Publish a new revision of the share ``name``; returns its number.

    ``data`` is the main file (bytes of the share's declared type).
    ``attachments`` maps keys to ``(bytes, mime)`` image pairs. Blobs the hub
    already holds are not uploaded again; the others are uploaded one at a
    time. With ``expected_revision`` the publish fails with ``invalid`` unless
    the current revision is that one.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    attachments = dict(attachments or {})
    blobs = {_sha(data): (data, _declared_type(name))}
    refs = {}
    for key, value in attachments.items():
        try:
            blob, mime = value
        except (TypeError, ValueError):
            raise TypeError(f"attachment {key!r} must be a (bytes, mime) pair") from None
        if not isinstance(blob, (bytes, bytearray)) or not isinstance(mime, str):
            raise TypeError(f"attachment {key!r} must be a (bytes, mime) pair")
        sha = _sha(blob)
        refs[key] = sha
        blobs.setdefault(sha, (blob, mime))

    hashes = list(blobs)
    missing = []
    for i in range(0, len(hashes), _MISSING_BATCH):
        res = _call("shares.missing", {"sha256": hashes[i:i + _MISSING_BATCH]})
        missing.extend(res.get("missing") or [])
    for sha in missing:
        if sha not in blobs:
            continue
        blob, mime = blobs[sha]
        res = _call("shares.put_blob", {"mime": mime, "data_b64": base64.b64encode(blob).decode("ascii")},
                    timeout=60)
        if res.get("sha256") != sha:
            raise SharesError("invalid", f"hub stored {res.get('sha256')!r}, expected {sha}")

    res = _call("shares.publish", {"name": name, "main": _sha(data), "attachments": refs,
                                   "expected_revision": expected_revision}, timeout=30)
    return int(res["revision"])


def _declared_type(name):
    """The MIME type ``name`` is declared with: from this app's spec, or else
    from the hub's list of this app's shares."""
    try:
        from .runtime import app_spec
        spec = app_spec()
    except Exception:  # noqa: BLE001
        spec = None
    if spec is not None:
        for share in spec.shares:
            if share["name"] == name:
                return share["type"]
        raise SharesError("invalid", f"share {name!r} is not declared in this app's PluginSpec.shares")
    app_id = os.environ.get(rpc.APP_ENV, "")
    for share in list_shares():
        if share.get("name") == name and (not app_id or share.get("provider") == app_id):
            return share.get("type")
    raise SharesError("invalid", f"share {name!r} is not declared by this app")


def list_shares():
    """Shares this app may open: those granted to it plus its own.

    Each is ``{"provider", "name", "title", "type", "revision", "updated"}``;
    ``revision`` is 0 until the first publish.
    """
    return list(_call("shares.list", {}).get("shares") or [])


def open_share(provider, name, if_revision=None):
    """The latest revision of ``provider``'s share ``name``.

    Returns None when ``if_revision`` is already the latest revision.
    """
    res = _call("shares.open", {"provider": provider, "name": name, "if_revision": if_revision},
                timeout=30)
    if res.get("not_modified"):
        return None
    try:
        data = base64.b64decode(res.get("data_b64") or "", validate=True)
    except (binascii.Error, ValueError):
        raise SharesError("invalid", "hub returned bad data_b64") from None
    return Opened(revision=int(res["revision"]), type=res.get("type") or "",
                  title=res.get("title") or "", data=data,
                  attachments=dict(res.get("attachments") or {}),
                  provider=provider, name=name)


def resolve(ref, opened, hub_origin):
    """The URL for an attachment reference ``bdh-share:<key>``.

    Returns None for anything else and for keys ``opened`` doesn't have.
    """
    if not isinstance(ref, str) or opened is None:
        return None
    ref = ref.strip()
    if not ref.startswith(REF_PREFIX):
        return None
    path = opened.attachments.get(ref[len(REF_PREFIX):])
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return (hub_origin or "").rstrip("/") + "/" + path.lstrip("/")
