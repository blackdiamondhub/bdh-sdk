"""Data an app provides to other parts of the hub.

An app lists capabilities in ``PluginSpec.provides``, mapping a capability
name to a dotted path of a zero-argument callable. The hub calls providers
in worker threads with a short deadline and keeps the last good result, so
a provider should return quickly from its own cache.

- ``stats``: iterable of :class:`Stat` (screensaver info strip)
- ``saver``: iterable of :class:`SaverImage` (screen saver pictures for the
  sources the app declares in ``PluginSpec.saver_sources``)
- ``backgrounds``: deprecated, the hub no longer reads it; use ``saver``
- ``season``: :class:`SeasonHint` or None (screensaver season and
  opening-day countdown)

Stats are listed on the settings page from ``PluginSpec.stat_keys`` without
calling the provider; a provider only returns stats whose key it declared.
Consumers refer to them as ``"<plugin id>.<key>"``.

Screen saver sources work the same way: each :class:`SaverSource` in
``PluginSpec.saver_sources`` is listed as a choice on the hub's Display
settings (with the app's title and description) without calling the app,
and the ``saver`` provider returns the pictures for them. A source key is
part of the hub's saved settings, so never rename one.

A ``live`` source is a set of live feeds such as webcams: the screen shows
one at a time and reloads it every ``refresh_seconds``; the hub wakes an
``on_demand`` app for it. A ``photos`` source is a deck of still pictures
that crossfade; the hub does not wake the app for it, so an app with a
``photos`` source should use ``lifecycle="always"``.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime

STATS = "stats"
BACKGROUNDS = "backgrounds"  # deprecated: the hub no longer reads it
SEASON = "season"
SAVER = "saver"
CAPABILITIES = frozenset({STATS, BACKGROUNDS, SEASON, SAVER})

LIVE = "live"
PHOTOS = "photos"
SAVER_KINDS = frozenset({LIVE, PHOTOS})
MAX_SAVER_SOURCES = 8
MAX_SAVER_IMAGES = 200
SAVER_TITLE_MAX = 32
SAVER_DESCRIPTION_MAX = 80
SAVER_TEXT_MAX = 120
SAVER_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")

WINTER = "winter"


@dataclass(frozen=True)
class Stat:
    """One labelled number or time, e.g. ``Stat("snow_24h", "New snow", "12 cm")``.

    Set exactly one of ``value`` (already formatted with units, in the app's
    own unit setting) or ``time`` (timezone-aware; the screen formats it in
    local time).
    """

    key: str
    label: str
    value: str | None = None
    time: datetime | None = None
    order: int = 100

    def __post_init__(self):
        if (self.value is None) == (self.time is None):
            raise ValueError(f"stat {self.key!r} needs exactly one of value or time")
        if self.time is not None and self.time.utcoffset() is None:
            raise ValueError(f"stat {self.key!r} time must be timezone-aware")


@dataclass(frozen=True)
class SaverSource:
    """A screen saver source an app offers, e.g.
    ``SaverSource("cams", "Mountain cams", "Live webcams from around Sun Peaks", LIVE)``.

    ``key`` is ``[a-z0-9][a-z0-9_-]*`` (at most 40 chars) and must never
    change once released. ``title`` is 1-32 chars, ``description`` at most
    80, ``kind`` is ``"live"`` or ``"photos"``.
    """

    key: str
    title: str
    description: str = ""
    kind: str = LIVE

    def __post_init__(self):
        if not isinstance(self.key, str) or not SAVER_KEY.fullmatch(self.key):
            raise ValueError(f"saver source key {self.key!r} must match {SAVER_KEY.pattern}")
        if not isinstance(self.title, str) or not 1 <= len(self.title.strip()) <= SAVER_TITLE_MAX:
            raise ValueError(f"saver source {self.key!r} title must be 1-{SAVER_TITLE_MAX} chars")
        if not isinstance(self.description, str) or len(self.description) > SAVER_DESCRIPTION_MAX:
            raise ValueError(
                f"saver source {self.key!r} description must be at most {SAVER_DESCRIPTION_MAX} chars")
        if self.kind not in SAVER_KINDS:
            raise ValueError(f"saver source {self.key!r} kind must be one of {sorted(SAVER_KINDS)}")


@dataclass(frozen=True)
class SaverImage:
    """One picture for a declared :class:`SaverSource`.

    ``image_url`` is a path on the app's own origin (e.g. ``"/webcams/cam/a.jpg"``);
    the hub serves it through its proxy and rejects absolute URLs. ``label``
    names the picture in the corner and in the settings picker (e.g. a
    camera name); ``caption`` is shown for ``photos`` sources when the admin
    turns captions on. A ``live`` image needs ``refresh_seconds`` (how often
    the screen reloads it; a stable URL is fine, the screen adds a
    cache-busting parameter). The hub keeps the first 200 per source.
    """

    source: str
    id: str
    image_url: str
    label: str = ""
    caption: str = ""
    refresh_seconds: int | None = None

    def __post_init__(self):
        if not self.id or not isinstance(self.id, str):
            raise ValueError("saver image needs an id")
        if not isinstance(self.image_url, str) or not self.image_url.startswith("/") \
                or self.image_url.startswith("//"):
            raise ValueError(f"saver image {self.id!r} image_url must be a path on the app's origin")
        if self.refresh_seconds is not None and (
                isinstance(self.refresh_seconds, bool) or not isinstance(self.refresh_seconds, int)
                or self.refresh_seconds < 1):
            raise ValueError(f"saver image {self.id!r} refresh_seconds must be at least 1")


@dataclass(frozen=True)
class Background:
    """Deprecated: the hub no longer reads ``backgrounds``; use :class:`SaverImage`.

    A live image a screen can show full-screen, e.g. a webcam.

    The screen reloads ``image_url`` every ``refresh_seconds`` (adding a
    cache-busting parameter), so a stable URL is fine.
    """

    id: str
    label: str
    image_url: str
    refresh_seconds: int = 60
    live: bool = True

    def __post_init__(self):
        if self.refresh_seconds < 1:
            raise ValueError(f"background {self.id!r} refresh_seconds must be at least 1")


@dataclass(frozen=True)
class SeasonHint:
    """Evidence about when a season starts.

    Without ``opens_on`` the hint means the season has started, e.g. the
    lifts are open; it can only bring the season forward (within the
    screen's early window), never delay it.

    With ``opens_on`` it is a schedule, e.g. the resort's published opening
    day: the season counts as started only from that date, and the screen
    counts down to it instead of its configured start date.

    Only ``"winter"`` is supported for now. ``reason`` is shown on the
    settings page.
    """

    season: str
    reason: str
    opens_on: date | None = None

    def __post_init__(self):
        if self.season != WINTER:
            raise ValueError(f"unsupported season {self.season!r}")
        if self.opens_on is not None and (
                isinstance(self.opens_on, datetime) or not isinstance(self.opens_on, date)):
            raise ValueError("opens_on must be a date")
