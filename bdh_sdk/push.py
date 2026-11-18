"""Live updates for an app's pages, pushed by the hub.

A page opens a websocket to ``/_bdh/push`` on its own origin. The hub owns
that socket: it asks the app which topics the visitor may hear, then sends the
page a small "something changed" message whenever the app publishes a matching
event. The page then refetches what it shows the usual way, so nothing private
travels over the socket.

Push is off until the app registers who may listen::

    from bdh_sdk import is_admin, push

    @push.authorize
    def who_hears(request):
        return ["cameras/#"] if is_admin(request) else []

and publishes with :func:`bdh_sdk.events.publish` on its own topics. Patterns
outside ``<app id>/#`` are dropped; the hub checks again.

The page side is ``{% static 'bdh/push.js' %}``.

This module does not import Django.
"""
from typing import Callable

from . import events

__all__ = ["authorize", "authorizer", "patterns_for", "MAX_PATTERNS"]

MAX_PATTERNS = 16

_authorizer: Callable | None = None


def authorize(fn: Callable) -> Callable:
    """Register ``fn(request) -> list of topic patterns`` the visitor may hear."""
    global _authorizer
    _authorizer = fn
    return fn


def authorizer() -> Callable | None:
    return _authorizer


def _own(app_id, pattern):
    if not isinstance(pattern, str):
        return False
    try:
        events._segments(pattern, wildcards=True)
    except ValueError:
        return False
    return pattern.startswith(app_id + "/")


def patterns_for(request, app_id: str) -> list[str] | None:
    """The patterns ``request``'s visitor may hear, or None when push is off."""
    fn = _authorizer
    if fn is None:
        return None
    wanted = fn(request) or ()
    return sorted({p for p in wanted if _own(app_id, p)})[:MAX_PATTERNS]
