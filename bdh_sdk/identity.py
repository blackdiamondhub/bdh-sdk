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
``X-BDH-Nonce``
    A random value, new for every request.
``X-BDH-Signature-V2``
    hex HMAC-SHA256, keyed with the app's token, over
    ``v2\\nmethod\\npath\\ntime\\nnonce\\nidentity\\nbody-hash``. ``path``
    includes the query string. ``body-hash`` is the hex SHA-256 of the body
    for the hub's own ``/_bdh/`` calls, ``-`` for proxied visitor requests
    (verifying those would make the app buffer every upload).
A signature is good once: the app remembers nonces for the
:data:`MAX_SKEW` window and refuses a repeat.

:class:`IdentityMiddleware` checks the signature and sets ``request.user``
to a :class:`HubUser` (or :data:`ANONYMOUS`) and ``request.bdh_mode`` /
``request.bdh_stay_key``. Requests with a bad or stale signature get 403, so
an app port reached directly (not through the hub) serves nothing.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

__all__ = [
    "ANONYMOUS", "HEADER_IDENTITY", "HEADER_NONCE", "HEADER_SIGNATURE_V2",
    "HEADER_TIME", "MAX_SKEW", "AnonymousHubUser", "HubUser", "IdentityMiddleware", "NonceCache",
    "app_token", "decode_identity", "encode_identity", "sign_v2", "signed_headers", "verify",
]

HEADER_IDENTITY = "X-BDH-Identity"
HEADER_TIME = "X-BDH-Time"
HEADER_NONCE = "X-BDH-Nonce"
HEADER_SIGNATURE_V2 = "X-BDH-Signature-V2"
# Seconds a signature stays valid; the hub and the app share a clock, and the
# hub signs each request just before sending it.
MAX_SKEW = 30
# Nonces remembered at once; far more than 2 * MAX_SKEW seconds of traffic.
MAX_NONCES = 10_000
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


def sign_v2(token, method, path, ts, nonce, identity="", digest=NO_BODY):
    msg = "\n".join(("v2", method.upper(), path, str(int(ts)), nonce, identity or "", digest))
    return hmac.new(_bytes(token), msg.encode(), hashlib.sha256).hexdigest()


def signed_headers(token, method, path, identity=None, body=None, now=None):
    """Headers the hub sends to an app. ``body`` (bytes) is signed when given."""
    ts = int(now if now is not None else time.time())
    ident = encode_identity(identity)
    digest = NO_BODY if body is None else body_hash(body)
    nonce = secrets.token_urlsafe(16)
    headers = {
        HEADER_TIME: str(ts),
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE_V2: sign_v2(token, method, path, ts, nonce, ident, digest),
    }
    if ident:
        headers[HEADER_IDENTITY] = ident
    return headers


class NonceCache:
    """Unexpired nonces, at most ``size`` of them (the oldest go first).

    In-process is enough: the runtime serves an app with one uvicorn worker.
    """

    def __init__(self, size=MAX_NONCES):
        self.size = size
        self._seen = OrderedDict()
        self._lock = threading.Lock()

    def add(self, nonce, expires, now=None):
        """Remember ``nonce`` until ``expires``; False if it was already seen."""
        with self._lock:
            now = time.time() if now is None else now
            while self._seen:
                oldest, until = next(iter(self._seen.items()))
                if until >= now and len(self._seen) < self.size:
                    break
                del self._seen[oldest]
            if nonce in self._seen:
                return False
            self._seen[nonce] = expires
            return True


#: The cache :func:`verify` uses unless given another.
NONCES = NonceCache()
def verify(token, method, path, headers, body=None, now=None, max_skew=MAX_SKEW, nonces=None):
    """The identity dict (``{}`` for anonymous) if ``headers`` carry a valid
    signature, else None. ``body`` (bytes) must match when given.

    The nonce is recorded in ``nonces`` (default :data:`NONCES`)
    and a repeat is refused.
    """
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
    nonce = headers.get(HEADER_NONCE) or ""
    signature_v2 = headers.get(HEADER_SIGNATURE_V2) or ""
    expected = sign_v2(token, method, path, ts, nonce, ident, digest)
    if not nonce or not hmac.compare_digest(expected, signature_v2):
        return None
    cache = NONCES if nonces is None else nonces
    if not cache.add(nonce, ts + max_skew, now=current):
        log.warning("refused a replayed hub signature for %s %s", method, path)
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
