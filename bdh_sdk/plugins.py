"""Plugin contract for Black Diamond Hub apps.

Every app (built-in or installed from its own repo) describes itself with a
single ``PluginSpec``. The hub reads these specs to add the app to
INSTALLED_APPS, mount its URLs and put its tiles on the homepage.

This module is the public surface that plugin repos import, so it must stay
free of Django model/settings imports: specs are loaded while settings are
still being built.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from importlib import import_module

log = logging.getLogger(__name__)

# 2: apps run in their own container and reach the hub through the broker.
API_VERSION = 2

# Container manifest. "none" has no network except the hub broker,
# "outbound" adds internet access through the egress proxy, "lan" runs on
# the host network (admin must approve; needed for multicast discovery).
NETWORK_NONE = "none"
NETWORK_OUTBOUND = "outbound"
NETWORK_LAN = "lan"
NETWORKS = frozenset({NETWORK_NONE, NETWORK_OUTBOUND, NETWORK_LAN})

# "always" apps keep running (background work, live events); "on_demand"
# apps are started by the first request and stopped when idle.
LIFECYCLE_ALWAYS = "always"
LIFECYCLE_ON_DEMAND = "on_demand"
LIFECYCLES = frozenset({LIFECYCLE_ALWAYS, LIFECYCLE_ON_DEMAND})

# How stale (seconds) an on_demand app's tile status, stats etc. may get
# before the hub wakes it to refresh them. 0 = the hub's default.
MIN_WAKE_EVERY = 5 * 60
MAX_WAKE_EVERY = 7 * 24 * 3600

DEFAULT_MEMORY_MB = 96
MIN_MEMORY_MB = 48
MAX_MEMORY_MB = 256

# Helper containers the platform knows how to run next to an app.
COMPANIONS = frozenset({"go2rtc", "aec"})

# Permissions an app can request; the admin approves a subset.
#   ha.read:<entity glob>        read states/history, e.g. ha.read:sensor.*
#   ha.call:<domain>.<service>   call a service, e.g. ha.call:light.turn_on
#   events.publish:<topic glob>  publish hub events
#   events.subscribe:<topic glob>
#   alerts, speakers, media      raise popups, announce, upload media to HA
#   displays.read                see which screens are checking in (bdh_sdk.displays)
_ENTITY_GLOB = r"[a-z0-9_*]+(\.[a-z0-9_*]+)?"
_TOPIC_GLOB = r"[A-Za-z0-9_./*#+-]+"
_PERMISSION_RE = re.compile(
    rf"(ha\.read:{_ENTITY_GLOB}"
    r"|ha\.call:[a-z0-9_]+\.([a-z0-9_]+|\*)"
    rf"|events\.(publish|subscribe):{_TOPIC_GLOB}"
    r"|alerts|speakers|media|displays\.read)"
)


def valid_permission(perm: str) -> bool:
    return isinstance(perm, str) and bool(_PERMISSION_RE.fullmatch(perm))

# Shares an app publishes for other apps (see bdh_sdk.shares).
_SHARE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_MIME_RE = re.compile(r"[a-z]+/[a-z0-9.+-]+")
SHARE_KEYS = frozenset({"name", "type", "title"})
MAX_SHARE_TITLE = 80


def valid_mime(mime: str) -> bool:
    return isinstance(mime, str) and bool(_MIME_RE.fullmatch(mime))

# Entry point group that separately installed apps register their spec under.
ENTRY_POINT_GROUP = "blackdiamondhub.plugins"

# Homepage modes. "guest" is used while a stay is active, "dashboard" when the
# lodge is between stays, "owner" when someone is signed in.
GUEST = "guest"
DASHBOARD = "dashboard"
OWNER = "owner"
ALL_MODES = frozenset({GUEST, DASHBOARD, OWNER})
OWNER_ONLY = frozenset({OWNER})

# Homepage sections, in display order.
SECTIONS = {
    "mountain": "On the mountain",
    "lodge": "In the lodge",
    "more": "More",
}


@dataclass(frozen=True)
class Tile:
    """A homepage tile that links to one of the app's pages.

    ``modes`` only controls where the tile is shown. Views behind an
    owner-only tile must still enforce sign-in themselves.
    """

    title: str
    url_name: str
    icon: str  # static path, e.g. "wifi/hub/icon.svg"
    section: str
    subtitle: str = ""
    order: int = 100
    modes: frozenset = ALL_MODES


# Status levels. "on" highlights something that is running (lights on, lifts
# open), "warn" flags a problem the user may want to look at.
INFO = "info"
ON = "on"
WARN = "warn"
STATUS_LEVELS = frozenset({INFO, ON, WARN})


@dataclass(frozen=True)
class Action:
    """A button on a status row or tile, handled by the ``run_action`` hook."""

    id: str
    icon: str  # Font Awesome classes, e.g. "fa-solid fa-pause"
    label: str = ""  # accessible name; the button shows only the icon
    primary: bool = False  # drawn in the accent colour


@dataclass(frozen=True)
class StatusRow:
    """One live item on a tile, e.g. a speaker group that is playing.

    ``image`` is a URL the screen's browser can load. ``dimmed`` greys the
    row out (paused, idle). With ``ends_at`` (epoch seconds) a ``{left}`` in
    ``detail`` becomes the time left (``4:05``) and the screen counts it down
    every second by itself.
    """

    id: str
    title: str
    detail: str = ""
    image: str = ""
    dimmed: bool = False
    actions: tuple[Action, ...] = ()
    ends_at: float | None = None


@dataclass(frozen=True)
class Status:
    """A one-line summary shown on the app's homepage tile.

    With ``rows`` the tile shows them, sized to fit the tile, each with its
    own buttons; ``actions`` are tile-wide buttons, shown when the tile is
    too small for rows. ``refresh`` (seconds) asks the homepage to check
    again that often while this status is showing, for things that change
    quickly; leave it None for the normal once a minute. ``ends_at`` counts
    down a ``{left}`` in ``text``, as for :class:`StatusRow`.

    With ``readings`` (and no ``rows``) the tile shows the first one big and
    the rest as small stats, e.g. the temperature up the mountain.
    """

    text: str
    level: str = INFO
    rows: tuple[StatusRow, ...] = ()
    actions: tuple[Action, ...] = ()
    refresh: int | None = None
    ends_at: float | None = None
    readings: tuple["Reading", ...] = ()


@dataclass(frozen=True)
class Shortcut:
    """A one-tap action shown in the homepage quick actions row.

    Shortcuts are owner-only unless the app lists other modes explicitly,
    because guest and dashboard modes don't require sign-in.
    """

    id: str
    title: str
    icon: str  # Font Awesome classes, e.g. "fa-solid fa-film"
    order: int = 100
    modes: frozenset = OWNER_ONLY


@dataclass(frozen=True)
class Stay:
    """The guests currently staying at the lodge.

    Use timezone-aware datetimes. Don't put door codes or other secrets here:
    the homepage shows these fields on a kiosk anyone can walk up to.
    """

    guest: str
    arrival: datetime | None = None
    checkout: datetime | None = None
    # The owners themselves are staying: the homepage acts as signed in.
    owner: bool = False


@dataclass(frozen=True)
class Reading:
    """A labelled value on the lodge card or a tile, e.g. ``Reading("Hot tub", 39, "C")``.

    Temperatures use unit ``"C"`` so the hub can show them in the visitor's
    chosen units.
    """

    label: str
    value: float | str
    unit: str = ""


@dataclass(frozen=True)
class Card:
    """The lodge card at the top of the homepage.

    ``alerts`` are problems worth acting on; tapping the card opens
    ``url_name``. The hub hides both from guests.
    """

    title: str
    detail: str = ""
    readings: tuple[Reading, ...] = ()
    alerts: tuple[str, ...] = ()
    url_name: str | None = None
    level: str = INFO


# Setting types an app can declare. The hub draws the matching control.
BOOL = "bool"
INT = "int"
STR = "str"
CHOICE = "choice"
TIME = "time"  # "HH:MM", 24-hour
MULTI = "multi"  # a list of values picked from the choices (checkboxes)
SECRET = "secret"  # write-only; stored encrypted, read with bdh_sdk.secrets.get
SETTING_TYPES = frozenset({BOOL, INT, STR, CHOICE, TIME, MULTI, SECRET})


def _call(path):
    module, _, name = path.rpartition(".")
    return getattr(import_module(module), name)()


def as_list(raw):
    """Parse a ``multi`` value: a list, a JSON list or comma-separated text.

    ``None`` (an unset setting) is an empty list.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        text = str(raw).strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError:
                raise ValueError("Must be a list.") from None
            if not isinstance(items, list):
                raise ValueError("Must be a list.")
        else:
            items = text.split(",")
    return list(dict.fromkeys(str(i).strip() for i in items if str(i).strip()))


@dataclass(frozen=True)
class Setting:
    """A setting the hub lets admins change on its Settings page.

    The saved value wins over the environment, so the environment (or
    ``default``) becomes the fallback. Read it with
    ``bdh_sdk.settings.app_setting(app_id, key, default)`` as usual.

    Passwords, API keys and tokens use ``type=SECRET``: the hub keeps them
    encrypted in its secret store, shows only whether one is set (never the
    value) and leaves them out of exports. Read one with
    ``bdh_sdk.secrets.get(key)``, not ``app_setting``. A secret has no
    ``default``.

    ``choices`` (for ``choice`` and ``multi``) is a tuple of values or
    ``(value, label)`` pairs. ``choices_from`` is instead the dotted path of a
    zero-argument callable returning them, for options only known at run
    time (e.g. the speakers on the network); it is called each time the
    Settings page is drawn. A ``multi`` setting is a list of picked values;
    with ``choices_from`` it may keep values the callable no longer returns,
    so a device that is offline right now stays picked.
    ``minimum``/``maximum`` bound ``int`` settings; ``unit`` is shown after
    the input (e.g. "min").
    """

    key: str
    label: str
    type: str = STR
    default: object = None
    help: str = ""
    choices: tuple = ()
    choices_from: str = ""
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""

    def __post_init__(self):
        if self.type not in SETTING_TYPES:
            raise ValueError(f"setting {self.key!r} has unknown type {self.type!r}")
        if self.type in (CHOICE, MULTI) and not (self.choices or self.choices_from):
            raise ValueError(f"{self.type} setting {self.key!r} needs choices or choices_from")
        if self.type == SECRET and self.default is not None:
            raise ValueError(f"secret setting {self.key!r} can't have a default")

    @property
    def options(self) -> tuple:
        """The choices as ``(value, label)`` pairs; ``()`` if ``choices_from`` fails."""
        choices = self.choices
        if self.choices_from:
            try:
                choices = tuple(_call(self.choices_from))
            except Exception:
                log.warning("choices for setting %r failed", self.key, exc_info=True)
                return ()
        return tuple(c if isinstance(c, tuple) else (c, str(c)) for c in choices)

    def parse(self, raw):
        """Turn form or environment input into a value of this setting's type.

        Raises ``ValueError`` with a message fit to show next to the field.
        """
        if self.type == BOOL:
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off", ""}:
                return False
            raise ValueError("Must be on or off.")
        if self.type == INT:
            try:
                value = int(str(raw).strip())
            except ValueError:
                raise ValueError("Must be a whole number.") from None
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"Must be at least {self.minimum}.")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"Must be at most {self.maximum}.")
            return value
        if self.type == MULTI:
            values = as_list(raw)
            if not self.choices_from:
                known = {str(v) for v, _label in self.options}
                if any(v not in known for v in values):
                    raise ValueError("Pick from the options.")
            return values
        if self.type == SECRET:
            value = str(raw)
            if len(value) > 8192:
                raise ValueError("Too long.")
            return value
        text = str(raw).strip()
        if self.type == CHOICE:
            for value, _label in self.options:
                if text == str(value):
                    return value
            raise ValueError("Pick one of the options.")
        if self.type == TIME:
            hours, sep, minutes = text.partition(":")
            if (sep and hours.isdigit() and minutes.isdigit() and len(minutes) == 2
                    and int(hours) < 24 and int(minutes) < 60):
                return f"{int(hours):02d}:{minutes}"
            raise ValueError("Must be a time like 23:00.")
        return text


@dataclass(frozen=True)
class SettingsPage:
    """A settings page the app draws itself, linked from its section on the
    hub's Settings page (for things a flat list of settings can't hold, like
    a list of calendar feeds).

    ``url_name`` is a Django URL name reversed by the hub (e.g.
    ``"calendar:feeds"``). The view must check ``bdh_sdk.auth.is_admin``
    itself. To share the hub's look, its template can extend
    ``hub_settings/_layout.html`` and fill ``{% block section %}``; the hub
    supplies the navigation context.
    """

    title: str
    url_name: str
    subtitle: str = ""
    icon: str = "fas fa-sliders"

    def __post_init__(self):
        if not self.title or not self.url_name:
            raise ValueError("SettingsPage needs a title and a url_name")


@dataclass(frozen=True)
class PluginSpec:
    """Describes one hub app.

    ``id`` is permanent: it names the URL prefix by default and will
    namespace the app's settings and data in future.

    The hooks are dotted paths so this module stays importable before
    Django is set up. They are resolved on first use:

    - ``status``: ``() -> Status | None``. Called in a worker thread with a
      timeout; return None when there is nothing worth showing.
    - ``shortcuts``: ``() -> iterable of Shortcut``.
    - ``run_shortcut``: ``(shortcut_id) -> str | None``. Runs a shortcut the
      hub has already checked is offered in the caller's mode. Raise to
      report failure; the optional return value is shown to the user.
    - ``run_action``: ``(row_id, action_id) -> str | None``. Runs a button
      from the app's current ``Status``; ``row_id`` is ``""`` for a
      tile-wide action. The hub has already checked the status offers it.
      Accepts an optional ``mode`` argument like ``status``.
    - ``websocket_routes``: a list of Channels URL patterns (e.g.
      ``"myapp.routing.websocket_urlpatterns"``). Imported by the ASGI
      application after Django is set up and mounted under the shared
      websocket router.
    - ``stay``: ``() -> Stay | None``. Reports the stay in progress, if any;
      the homepage is in guest mode while one is reported. Called like
      ``status`` (worker thread, timeout, cached briefly).
    - ``card``: ``() -> Card | None``. The lodge card shown at the top of
      the homepage in every mode; the first app that provides one is used.
      Called like ``status``.

    ``settings`` lists the :class:`Setting` objects admins can change on the
    hub's Settings page; the app gets its own section there when non-empty.
    ``settings_pages`` lists :class:`SettingsPage` links shown in that
    section too (the app gets a section if either is non-empty).

    ``provides`` maps capability names from :mod:`bdh_sdk.provides` (e.g.
    ``"stats"``) to dotted paths of zero-argument callables, for data other
    parts of the hub can use. ``stat_keys`` lists the ``(key, label)`` of
    every stat the app may provide. ``saver_sources`` lists the
    :class:`~bdh_sdk.provides.SaverSource` choices the app offers the screen
    saver (at most 8, unique keys), with pictures from its ``saver``
    provider. An app needs no tiles or URLs: it can just provide data and
    settings.

    The container manifest: ``permissions`` lists what the app asks the hub
    broker for (see :func:`valid_permission`); the admin grants a subset.
    ``network`` is one of :data:`NETWORKS`, ``lifecycle`` one of
    :data:`LIFECYCLES`, ``memory_mb`` the container memory limit and
    ``companions`` names platform helper containers: ``"go2rtc"`` (its API
    URL arrives as ``BDH_GO2RTC_URL``) and ``"aec"`` (doorbell echo
    cancellation, ``BDH_AEC_URL``; needs ``network="lan"`` and ``"go2rtc"``).
    ``wake_every`` (seconds, 0 = hub default) is how old an ``on_demand``
    app's cached status/stats may get before the hub wakes it to refresh
    them; while it sleeps the hub shows the last answer.

    ``shares`` declares the files the app publishes for other apps with
    :mod:`bdh_sdk.shares`: dicts with ``name`` (``[a-z0-9][a-z0-9_-]*``, at
    most 40 chars, unique), ``type`` (the main file's MIME type) and
    ``title`` (1-80 chars). ``reads`` lists the MIME types the app can
    consume; the hub only uses it to suggest consumers when the admin grants
    a share. Neither is a permission.
    """

    id: str
    app: str  # Django app module or AppConfig path for INSTALLED_APPS
    urls: str | None = None  # URLconf module, mounted at url_prefix
    url_prefix: str | None = None  # defaults to "<id>/"
    tiles: tuple[Tile, ...] = field(default_factory=tuple)
    status: str | None = None
    shortcuts: str | None = None
    run_shortcut: str | None = None
    run_action: str | None = None
    websocket_routes: str | None = None
    stay: str | None = None
    card: str | None = None
    settings: tuple[Setting, ...] = field(default_factory=tuple)
    settings_pages: tuple[SettingsPage, ...] = field(default_factory=tuple)
    provides: dict[str, str] = field(default_factory=dict, hash=False)
    stat_keys: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    saver_sources: tuple = field(default_factory=tuple)
    api_version: int = API_VERSION
    permissions: tuple[str, ...] = field(default_factory=tuple)
    network: str = NETWORK_OUTBOUND
    lifecycle: str = LIFECYCLE_ON_DEMAND
    memory_mb: int = DEFAULT_MEMORY_MB
    companions: tuple[str, ...] = field(default_factory=tuple)
    wake_every: int = 0
    shares: tuple[dict, ...] = field(default_factory=tuple, hash=False)
    reads: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        self._check_shares()
        keys = [key for key, _label in self.stat_keys]
        if len(keys) != len(set(keys)):
            raise ValueError(f"plugin {self.id!r} has duplicate stat_keys")
        if any(not key or not label for key, label in self.stat_keys):
            raise ValueError(f"plugin {self.id!r} stat_keys need a key and a label")
        self._check_saver_sources()
        for perm in self.permissions:
            if not valid_permission(perm):
                raise ValueError(f"plugin {self.id!r} has invalid permission {perm!r}")
        if self.network not in NETWORKS:
            raise ValueError(f"plugin {self.id!r} network must be one of {sorted(NETWORKS)}")
        if self.lifecycle not in LIFECYCLES:
            raise ValueError(f"plugin {self.id!r} lifecycle must be one of {sorted(LIFECYCLES)}")
        if not isinstance(self.memory_mb, int) or not MIN_MEMORY_MB <= self.memory_mb <= MAX_MEMORY_MB:
            raise ValueError(
                f"plugin {self.id!r} memory_mb must be {MIN_MEMORY_MB}..{MAX_MEMORY_MB}"
            )
        for name in self.companions:
            if name not in COMPANIONS:
                raise ValueError(f"plugin {self.id!r} has unknown companion {name!r}")
        if "aec" in self.companions and (self.network != NETWORK_LAN or "go2rtc" not in self.companions):
            raise ValueError(f"plugin {self.id!r} companion 'aec' needs network 'lan' and 'go2rtc'")
        if (not isinstance(self.wake_every, int) or isinstance(self.wake_every, bool)
                or not (self.wake_every == 0 or MIN_WAKE_EVERY <= self.wake_every <= MAX_WAKE_EVERY)):
            raise ValueError(
                f"plugin {self.id!r} wake_every must be 0 or {MIN_WAKE_EVERY}..{MAX_WAKE_EVERY} seconds"
            )

    def _check_saver_sources(self):
        from .provides import MAX_SAVER_SOURCES, SAVER, SaverSource

        if isinstance(self.saver_sources, (str, bytes, dict)):
            raise ValueError(f"plugin {self.id!r} saver_sources must be a list")
        sources = tuple(self.saver_sources)
        if not all(isinstance(src, SaverSource) for src in sources):
            raise ValueError(f"plugin {self.id!r} saver_sources must be SaverSource objects")
        if len(sources) > MAX_SAVER_SOURCES:
            raise ValueError(f"plugin {self.id!r} has more than {MAX_SAVER_SOURCES} saver_sources")
        if len({src.key for src in sources}) != len(sources):
            raise ValueError(f"plugin {self.id!r} has duplicate saver_sources keys")
        if SAVER in self.provides and not sources:
            raise ValueError(f"plugin {self.id!r} provides saver images but declares no saver_sources")
        object.__setattr__(self, "saver_sources", sources)

    def _check_shares(self):
        if isinstance(self.shares, (str, bytes, dict)) or isinstance(self.reads, (str, bytes)):
            raise ValueError(f"plugin {self.id!r} shares and reads must be lists")
        shares = []
        for share in self.shares:
            if not isinstance(share, dict) or set(share) != SHARE_KEYS:
                raise ValueError(
                    f"plugin {self.id!r} shares must be dicts with exactly {sorted(SHARE_KEYS)}"
                )
            name, mime, title = share["name"], share["type"], share["title"]
            if not isinstance(name, str) or not _SHARE_NAME_RE.fullmatch(name):
                raise ValueError(f"plugin {self.id!r} has invalid share name {name!r}")
            if not valid_mime(mime):
                raise ValueError(f"plugin {self.id!r} share {name!r} has invalid type {mime!r}")
            if not isinstance(title, str) or not 1 <= len(title) <= MAX_SHARE_TITLE:
                raise ValueError(
                    f"plugin {self.id!r} share {name!r} title must be 1-{MAX_SHARE_TITLE} chars"
                )
            shares.append({"name": name, "type": mime, "title": title})
        names = [s["name"] for s in shares]
        if len(names) != len(set(names)):
            raise ValueError(f"plugin {self.id!r} has duplicate share names")
        for mime in self.reads:
            if not valid_mime(mime):
                raise ValueError(f"plugin {self.id!r} has invalid reads type {mime!r}")
        object.__setattr__(self, "shares", tuple(shares))
        object.__setattr__(self, "reads", tuple(self.reads))

    def manifest(self) -> dict:
        """The container/runtime facts the store and supervisor need."""
        return {
            "id": self.id,
            "api_version": self.api_version,
            "permissions": sorted(set(self.permissions)),
            "network": self.network,
            "lifecycle": self.lifecycle,
            "memory_mb": self.memory_mb,
            "companions": list(self.companions),
            "wake_every": self.wake_every,
            "provides": sorted(self.provides),
            "shares": [dict(s) for s in self.shares],
            "reads": sorted(set(self.reads)),
        }

    @property
    def prefix(self) -> str:
        return self.url_prefix if self.url_prefix is not None else f"{self.id}/"
