"""Pop-up alerts on the hub's screens.

An app pushes an :class:`Alert` and every open hub page (any mode, any page)
shows it in a modal: a title, an optional line of text and an ``<iframe>`` of
``embed_url``. The hub wakes the screensaver for it and closes it after
``ttl`` seconds. Pushing again with the same ``key`` while it is showing just
restarts the timer, and a different alert replaces the one on screen.

A new alert plays ``sound``: ``None`` for the hub's default chime,
:data:`SILENT` for none, one of :data:`CHIMES`, or a path to an audio file
on the hub. Set ``speak`` and the screens then read it out (Home Assistant
text-to-speech, see :func:`bdh_sdk.speakers.tts_audio`). Repeats with the
same ``key`` stay quiet unless ``resound`` is set, and the hub's night window
is always quiet.

:func:`dismiss` closes an alert on every screen, e.g. once it has been dealt
with somewhere else.

The hub installs the delivery functions with :func:`set_sender`; without them
(unit tests, scripts) pushing only logs at debug level. :func:`push` and
:func:`dismiss` are safe to call from any thread, including one running its
own event loop, and never block.

This module does not import Django at import time.
"""
import logging
import re
from dataclasses import asdict, dataclass

__all__ = ["CHIMES", "SILENT", "Alert", "apush", "dismiss", "push", "set_sender"]

log = logging.getLogger(__name__)

MAX_TTL = 600
MAX_SPEAK = 300

_APP_ID = re.compile(r"^[a-z0-9_]+$")

# Built-in chimes the hub can play, in the order its pickers list them.
CHIMES = (
    "ding-dong", "soft-bell", "rising-trio", "notify", "westminster", "glass-ping",
    "low-pulse", "knock-knock", "ding-dong-ding", "harp-sweep", "kalimba", "single-ding",
)
SILENT = "none"


def _hub_path(value):
    return isinstance(value, str) and value.startswith("/") and not value.startswith("//") and "\\" not in value


@dataclass(frozen=True)
class Alert:
    app: str
    key: str
    title: str
    embed_url: str
    body: str = ""
    ttl: int = 60
    wake: bool = True
    sound: str | None = None
    speak: str = ""
    resound: bool = False

    def __post_init__(self):
        if not isinstance(self.app, str) or not _APP_ID.match(self.app):
            raise ValueError(f"invalid app id: {self.app!r}")
        if not self.key or not isinstance(self.key, str):
            raise ValueError("alert key is required")
        if not self.title or not isinstance(self.title, str):
            raise ValueError("alert title is required")
        # Same-origin paths only: the hub loads this in an iframe on every screen.
        if not _hub_path(self.embed_url):
            raise ValueError(f"embed_url must be a path on the hub, got {self.embed_url!r}")
        if isinstance(self.ttl, bool) or not isinstance(self.ttl, int) or not 1 <= self.ttl <= MAX_TTL:
            raise ValueError(f"ttl must be 1-{MAX_TTL} seconds, got {self.ttl!r}")
        snd = self.sound
        if snd is not None and snd != SILENT and snd not in CHIMES and not _hub_path(snd):
            raise ValueError(f"sound must be None, {SILENT!r}, a chime name or a path on the hub, got {snd!r}")
        if not isinstance(self.speak, str) or len(self.speak) > MAX_SPEAK:
            raise ValueError(f"speak must be text of at most {MAX_SPEAK} characters")

    def as_dict(self):
        return asdict(self)


_sender = None
_dismisser = None


def set_sender(sender, dismisser=None):
    """Install the hub's delivery functions; ``None`` removes them.

    ``sender(alert)`` and ``dismisser(app, key)`` must be thread-safe and must
    not block.
    """
    global _sender, _dismisser
    _sender = sender
    _dismisser = dismisser


def push(alert: Alert) -> None:
    """Show ``alert`` on the hub's screens."""
    if not isinstance(alert, Alert):
        raise TypeError(f"expected an Alert, got {type(alert).__name__}")
    if _sender is None:
        log.debug("no alert sender installed; dropping %s", alert.key)
        return
    _sender(alert)


async def apush(alert: Alert) -> None:
    """:func:`push` for async code; it doesn't block either."""
    push(alert)

def dismiss(app: str, key: str) -> None:
    """Close the alert ``key`` from ``app`` on every screen showing it."""
    if not isinstance(app, str) or not _APP_ID.match(app):
        raise ValueError(f"invalid app id: {app!r}")
    if not key or not isinstance(key, str):
        raise ValueError("alert key is required")
    if _dismisser is None:
        log.debug("no alert dismisser installed; dropping %s", key)
        return
    _dismisser(app, key)
