"""Websockets for an app in its container.

The hub proxies a visitor's websocket to the app like any request: it signs
the handshake (``GET`` + path, no body) with the app token and the visitor's
identity. :class:`HubIdentity` checks that signature and sets
``scope["user"]`` (plus ``bdh_mode``, ``bdh_stay_key``, ``bdh_hub_origin``),
then hands the connection to the app's ``websocket_routes``: Channels URL
patterns, matched against the full path as in the hub (include the app's
prefix, e.g. ``path("myapp/live", consumer)``).
"""
from importlib import import_module

from django.urls import re_path
from django.utils.encoding import escape_uri_path, iri_to_uri

from ..identity import INTERNAL_PREFIX, user_for, verify

__all__ = ["HubIdentity", "application", "signed_path"]


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def signed_path(scope):
    """The handshake path as the hub signed it (``request.get_full_path()``)."""
    path = escape_uri_path(scope.get("path") or "/")
    query = (scope.get("query_string") or b"").decode("utf-8", "replace")
    return path + ("?" + iri_to_uri(query) if query else "")


async def close(receive, send, code=4403):
    """Refuse a websocket that hasn't been accepted."""
    await receive()  # websocket.connect
    await send({"type": "websocket.close", "code": code})


class HubIdentity:
    """ASGI middleware: refuse websockets the hub didn't sign (4403)."""

    def __init__(self, inner, token=None):
        import os

        self.inner = inner
        self.token = os.environ.get("BDH_APP_TOKEN", "") if token is None else token

    async def __call__(self, scope, receive, send):
        headers = _Headers((k.decode("latin1").lower(), v.decode("latin1"))
                           for k, v in scope.get("headers") or ())
        path = signed_path(scope)
        ident = None
        if not path.startswith(INTERNAL_PREFIX):
            ident = verify(self.token, "GET", path, headers)
        if ident is None:
            return await close(receive, send)
        scope = dict(scope, user=user_for(ident),
                     bdh_mode=str(ident.get("mode") or "guest"),
                     bdh_stay_key=str(ident.get("stay") or ""),
                     bdh_hub_origin=str(ident.get("o") or ""))
        return await self.inner(scope, receive, send)


async def _not_found(scope, receive, send):
    await close(receive, send, 4404)


def routes(spec):
    """The app's websocket URL patterns, then a catch-all that closes with 4404."""
    patterns = []
    if spec.websocket_routes:
        module, _, attr = spec.websocket_routes.rpartition(".")
        patterns = list(getattr(import_module(module), attr))
    return patterns + [re_path(r"", _not_found)]


def application(spec, token=None):
    from channels.routing import URLRouter

    return HubIdentity(URLRouter(routes(spec)), token=token)
