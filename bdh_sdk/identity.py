"""Who is asking, for apps running in their own container.

The hub's proxy is the only way in to an app. It drops the hub's cookies and
``Authorization`` header and tells the app who the visitor is in signed
headers instead:

``X-BDH-Identity``
    base64url JSON ``{"u": username, "n": first name, "staff": bool,
    "mode": "guest"|"owner"|..., "stay": stay key, "o": the hub origin the
    visitor is using}``; ``u``/``n``/``staff`` are absent for anonymous visitors.
``X-BDH-Time``
    Unix seconds when the hub signed the request.
``X-BDH-Signature``
    hex HMAC-SHA256, keyed with the app's token, over
    ``method\\npath\\ntime\\nidentity\\nbody-hash``. ``body-hash`` is the hex
    SHA-256 of the body for the hub's own ``/_bdh/`` calls, ``-`` for
    proxied visitor requests (they stream).

:class:`IdentityMiddleware` checks the signature and sets ``request.user``
to a :class:`HubUser` (or :data:`ANONYMOUS`) and ``request.bdh_mode`` /
``request.bdh_stay_key``. Requests with a bad or stale signature get 403, so
an app port reached directly (not through the hub) serves nothing.
"""
import base64
import hashlib
import hmac
import json
import os
import time

__all__ = [
    "ANONYMOUS", "HEADER_IDENTITY", "HEADER_SIGNATURE", "HEADER_TIME", "MAX_SKEW",
    "AnonymousHubUser", "HubUser", "IdentityMiddleware", "app_token", "decode_identity",
    "encode_identity", "sign", "signed_headers", "verify",
]

HEADER_IDENTITY = "X-BDH-Identity"
HEADER_TIME = "X-BDH-Time"
HEADER_SIGNATURE = "X-BDH-Signature"
# Seconds a signature stays valid; the hub and the app share a clock.
MAX_SKEW = 60
NO_BODY = "-"
INTERNAL_PREFIX = "/_bdh/"


def app_token(secret_key, app_id, generation):
    """The token the hub gives ``app_id``: derived, so the hub stores nothing
    but the generation and can rotate it by bumping that."""
    msg = f"bdh-app:{app_id}:{int(generation)}".encode()
    return hmac.new(_bytes(secret_key), msg, hashlib.sha256).hexdigest()


def _bytes(value):
    return value if isinstance(value, bytes) else str(value).encode()


def body_hash(body):
    return hashlib.sha256(body or b"").hexdigest()


def encode_identity(identity):
    if not identity:
        return ""
    raw = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_identity(value):
    if not value:
        return None
    pad = "=" * (-len(value) % 4)
    data = json.loads(base64.urlsafe_b64decode(value + pad))
    if not isinstance(data, dict):
        raise ValueError("identity is not an object")
    return data


def sign(token, method, path, ts, identity="", digest=NO_BODY):
    msg = "\n".join((method.upper(), path, str(int(ts)), identity or "", digest))
    return hmac.new(_bytes(token), msg.encode(), hashlib.sha256).hexdigest()


def signed_headers(token, method, path, identity=None, body=None, now=None):
    """Headers the hub sends to an app. ``body`` (bytes) is signed when given."""
    ts = int(now if now is not None else time.time())
    ident = encode_identity(identity)
    digest = NO_BODY if body is None else body_hash(body)
    headers = {HEADER_TIME: str(ts), HEADER_SIGNATURE: sign(token, method, path, ts, ident, digest)}
    if ident:
        headers[HEADER_IDENTITY] = ident
    return headers


def verify(token, method, path, headers, body=None, now=None, max_skew=MAX_SKEW):
    """The identity dict (``{}`` for anonymous) if ``headers`` carry a valid
    signature, else None. ``body`` (bytes) must match when given."""
    if not token:
        return None
    try:
        ts = int(headers.get(HEADER_TIME) or "")
    except ValueError:
        return None
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew:
        return None
    ident = headers.get(HEADER_IDENTITY) or ""
    digest = NO_BODY if body is None else body_hash(body)
    expected = sign(token, method, path, ts, ident, digest)
    if not hmac.compare_digest(expected, headers.get(HEADER_SIGNATURE) or ""):
        return None
    try:
        return decode_identity(ident) or {}
    except (ValueError, TypeError):
        return None


class AnonymousHubUser:
    """A visitor who isn't signed in to the hub."""
    is_authenticated = False
    is_anonymous = True
    is_active = False
    is_staff = False
    is_superuser = False
    username = ""
    first_name = ""
    pk = id = None

    def get_username(self):
        return self.username

    def has_perm(self, perm, obj=None):
        return False

    def __str__(self):
        return "AnonymousUser"


ANONYMOUS = AnonymousHubUser()


class HubUser(AnonymousHubUser):
    """A signed-in hub user, as vouched for by the hub. Not a model: apps
    can't store references to it, only its ``username``."""
    is_authenticated = True
    is_anonymous = False
    is_active = True

    def __init__(self, username, first_name="", is_staff=False):
        self.username = username
        self.first_name = first_name
        self.is_staff = bool(is_staff)
        self.is_superuser = self.is_staff

    def has_perm(self, perm, obj=None):
        return self.is_staff

    def __str__(self):
        return self.username

    def __eq__(self, other):
        return isinstance(other, HubUser) and other.username == self.username

    def __hash__(self):
        return hash(("HubUser", self.username))


def user_for(identity):
    if identity and identity.get("u"):
        return HubUser(str(identity["u"]), str(identity.get("n") or ""), bool(identity.get("staff")))
    return ANONYMOUS


class IdentityMiddleware:
    """Django middleware: verify the hub's signature, set ``request.user``.

    Reads the token from ``BDH_APP_TOKEN``. ``/_bdh/`` paths must also sign
    the body; everything else is a proxied visitor request.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.token = os.environ.get("BDH_APP_TOKEN", "")

    def __call__(self, request):
        path = request.get_full_path()
        internal = request.path.startswith(INTERNAL_PREFIX)
        body = request.body if internal else None
        identity = verify(self.token, request.method, path, request.headers, body=body)
        if identity is None:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("not signed by the hub")
        request.user = user_for(identity)
        request.bdh_internal = internal
        request.bdh_mode = str(identity.get("mode") or "guest")
        request.bdh_stay_key = str(identity.get("stay") or "")
        request.bdh_hub_origin = str(identity.get("o") or "")
        return self.get_response(request)
