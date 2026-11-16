# bdh-sdk

The Black Diamond Hub SDK: the small contract package that hub apps import to
describe themselves to [Black Diamond Hub](https://github.com/blackdiamondlodge/BlackDiamondHub).

Hub apps are Django apps that live in their own repos (e.g. `bdh-app-wifi`)
and register a `blackdiamondhub.plugins` entry point that resolves to a
`PluginSpec`. `bdh-sdk` has no runtime dependencies and never imports or
configures Django on import, so app `plugin.py` modules can use it while the
hub's settings are still being built.

See [docs/app-template.md](docs/app-template.md) for the layout of an app repo.

## Install

`bdh-sdk` is not published to PyPI. Install a tagged release from GitHub:

```
pip install "bdh-sdk @ git+https://github.com/blackdiamondlodge/bdh-sdk@v0.1.0"
```

or download the wheel attached to the GitHub release (its sha256 is in the
release notes). Apps depend on `bdh-sdk>=0.1,<1`.

## Plugin contract

```python
from bdh_sdk import PluginSpec, Tile, OWNER_ONLY

spec = PluginSpec(
    id="wifi",
    app="wifi",
    urls="wifi.urls",              # mounted at "wifi/" (spec.prefix)
    tiles=(Tile(title="Wi-Fi", url_name="wifi:index",
                icon="wifi/hub/icon.svg", section="lodge"),),
    status="wifi.homepage.status",
    websocket_routes="wifi.routing.websocket_urlpatterns",
)
```

`bdh_sdk` exports `API_VERSION`, `ENTRY_POINT_GROUP`
(`"blackdiamondhub.plugins"`), the homepage modes `GUEST`, `DASHBOARD`,
`OWNER`, `ALL_MODES`, `OWNER_ONLY`, `SECTIONS`, the status levels `INFO`,
`ON`, `WARN`, `STATUS_LEVELS`, and the frozen dataclasses `PluginSpec`,
`Tile`, `Status`, `StatusRow`, `Action`, `Shortcut`, `Stay`, `Card`, `Reading` and
`Setting`. Hooks (`status`, `shortcuts`, `run_shortcut`, `run_action`,
`websocket_routes`, `stay`, `card`) are dotted
`module.attribute` paths that the hub resolves lazily.

A `status` hook can return live `rows` (`StatusRow`: title, detail,
image URL, `dimmed`, and `Action` buttons) instead of just a line of text,
e.g. the speaker groups that are playing. The tile shows as many as fit its
size; tile-wide `actions` are used when it is too small for rows. Taps call
`run_action(row_id, action_id)` (`row_id` is `""` for tile-wide actions).
Set `Status.refresh` (seconds) for things that change quickly: the
homepage checks that often while the status is showing.
With `Status.readings` (`Reading`s, no rows) the tile shows the first one
big and the rest as small stats; unit `"C"` follows the hub's °C/°F toggle.

A `stay` hook returns the `Stay` in progress (guest name, arrival, checkout)
or None. While an app reports a stay the homepage is in guest mode and
welcomes the guest by name. Never put door codes in it: the homepage is a
kiosk. A stay with `owner=True` is an owner visit: the homepage acts as
signed in until it ends.

A `card` hook returns the lodge `Card` shown at the top of the homepage in
every mode (title, detail line, `Reading`s such as temperatures, and
alerts). Tapping it opens `url_name`; guests see neither alerts nor the
link.

## Provided data

Apps can offer data to other parts of the hub (today: the screensaver) with
`provides`, a map from capability to a zero-argument callable. An app with
no tiles or URLs that only provides data and settings is fine.

```python
from bdh_sdk import PluginSpec, STATS, Stat

spec = PluginSpec(
    id="weather",
    app="weather",
    provides={STATS: "weather.provide.stats"},
    stat_keys=(("temp", "Temperature"), ("sunrise", "Sunrise")),
)

# weather/provide.py
def stats():
    now = cached_conditions()            # return quickly, from your own cache
    yield Stat("temp", "Temperature", value=f"{now.temp:.0f}°C")
    yield Stat("sunrise", "Sunrise", time=now.sunrise)   # timezone-aware
```

| capability | returns | used for |
|---|---|---|
| `STATS` (`"stats"`) | iterable of `Stat` | screensaver info strip |
| `BACKGROUNDS` (`"backgrounds"`) | iterable of `Background` | screensaver live cam |
| `SEASON` (`"season"`) | `SeasonHint` or None | bringing winter forward (e.g. lifts open) |

- `Stat` has exactly one of `value` (formatted, with units) or `time`
  (timezone-aware). Declare every key in `stat_keys`: the settings page
  lists them without calling the app, and undeclared keys are dropped.
  Other code refers to a stat as `"<plugin id>.<key>"`, e.g.
  `weather.sunrise`.
- `Background(id, label, image_url, refresh_seconds=60)`: the screen
  reloads the URL every `refresh_seconds`, so a stable URL is fine.
- `SeasonHint("winter", reason)` can only bring winter forward.

The hub calls providers in worker threads with a short deadline and keeps
the last good result, so a slow upstream never stalls a screen.

## App settings

```python
from bdh_sdk.settings import app_setting, as_bool

ssid = app_setting("wifi", "ssid", legacy=["WIFI_SSID"])
show_qr = app_setting("wifi", "show_qr", True, cast=as_bool)
```

`app_setting(app_id, key, default=..., *, legacy=(), cast=None)` looks the
setting up in this order and returns the first hit:

0. The value saved on the hub's Settings page. The hub installs a store
   with `bdh_sdk.settings.set_store(lookup)`; `lookup(app_id, key)` returns
   the saved value or raises `KeyError`.
1. Environment variable `BDH_<APPID>_<KEY>`: upper-cased, every
   non-alphanumeric character replaced by `_` (`("ski-cams", "base.url")` ->
   `BDH_SKI_CAMS_BASE_URL`).
2. The first environment variable in `legacy` that is set (old
   un-namespaced names such as `WIFI_SSID`).
3. Django settings, only if Django is installed **and** configured:
   `settings.BDH_<APPID>_<KEY>`, then each `legacy` name.
4. `default`.

An environment variable counts as set when it is present, even if empty.
With no value and no default, `bdh_sdk.settings.MissingSetting` (a
`KeyError`) is raised. `cast` is applied only to string values; `as_bool`
accepts `1/true/yes/on` and `0/false/no/off` (case-insensitive).

### Settings page

Declare the settings admins may change from the hub, and the hub gives the
app a section on its staff-only Settings page:

```python
from bdh_sdk import PluginSpec, Setting
from bdh_sdk.plugins import BOOL, INT

spec = PluginSpec(
    id="wifi", app="wifi",
    settings=(
        Setting("show_qr", "Show QR code", type=BOOL, default=True),
        Setting("refresh", "Refresh every", type=INT, default=5,
                minimum=1, maximum=60, unit="min"),
    ),
)
```

Types are `BOOL`, `INT` (optional `minimum`/`maximum`), `STR`, `CHOICE`
(`choices` of values or `(value, label)` pairs), `MULTI` (a list of values
picked from `choices`, drawn as checkboxes), `TIME` (`"HH:MM"`) and `SECRET`
(see [Secrets](#secrets)). For
options only known at run time, pass `choices_from="myapp.module.func"`
instead of `choices`: a zero-argument callable returning the same pairs,
called whenever the Settings page is drawn. A `MULTI` setting with
`choices_from` keeps picked values the callable no longer returns (e.g. a
speaker that is offline). In the environment a `MULTI` value is a JSON list
or comma-separated text; read it with `app_setting(..., cast=as_list)`
(`from bdh_sdk.plugins import as_list`).
`Setting.parse(raw)` validates input and raises `ValueError` with a message
for the form. Saved values win over the environment, so keep reading them
with `app_setting`. Passwords, API keys and tokens are `SECRET` settings,
read with `bdh_sdk.secrets.get`, never `app_setting`.

For anything a flat list can't hold (say, a list of calendar feeds), the
app can draw its own pages and list them in `settings_pages`; the hub links
them from the app's section:

```python
from bdh_sdk import SettingsPage

settings_pages=(SettingsPage("Feeds", "calendar:feeds", subtitle="iCal links"),)
```

`url_name` is reversed by the hub. The view must check `is_admin(request)`
itself. Its template can `{% extends "hub_settings/_layout.html" %}` and fill
`{% block section %}` to sit inside the hub's Settings navigation.

## App state and media

Apps that need to remember something between runs (not a setting a person
edits) use a per-app key/value store. Values must be JSON-serializable:

```python
from bdh_sdk.storage import app_state

state = app_state("display")
opened = state.get("lifts_opened")          # None when unset
state.set("lifts_opened", "2026-11-21")
state.delete("lifts_opened"); state.keys(); "lifts_opened" in state
```

The hub installs a database backend with
`bdh_sdk.storage.set_state_backend(backend)` (`get` raising `KeyError`,
`set`, `delete`, `keys`). Without one, state lives in memory, which is what
unit tests get.

Files an app stores go in its own folder via a Django storage rooted at
`MEDIA_ROOT/apps/<app_id>/` (served at `MEDIA_URL + "apps/<app_id>/"`):

```python
from bdh_sdk.storage import app_media

storage = app_media("display")
name = storage.save("photos/lake.jpg", uploaded_file)
url = storage.url(name)
```

App ids must be lower-case letters, digits and `_`.

## Secrets

Passwords, API keys and tokens live in the hub's secret store, encrypted
(AES-256-GCM, key kept outside the database) and owned by one app. Declare
the ones an admin enters as `SECRET` settings; the hub's Settings page then
shows only "set" / "not set" with Replace and Clear, never the value, and
leaves them out of exports, logs and the audit log (which records only app,
name and operation).

```python
from bdh_sdk import Setting, secrets
from bdh_sdk.plugins import SECRET

settings=(Setting("protect_api_key_1", "Protect API key", type=SECRET),)

api_key = secrets.get("protect_api_key_1")      # None when unset
secrets.set("refresh_token", new_token)          # e.g. a rotated OAuth token
secrets.delete("refresh_token")
```

- In a container (`bdh_sdk.rpc.install`) calls go to the broker as
  `secrets.get` / `secrets.set` / `secrets.delete`. The broker takes the app
  from the token: an app can only reach its own secrets (like its state, no
  extra permission is needed), and `app=` is not sent.
- In-process, the hub installs a database backend with
  `secrets.set_backend(backend)` (`get` raising `KeyError`, `set`, `delete`)
  and the app passes its id: `secrets.get("api_key", app="cameras")`.
  Without a backend, secrets live in memory (what unit tests get).
- `get(name, default=None, *, app=None, legacy=())` falls back to
  `BDH_<APP>_<NAME>`, then any `legacy` env names, so a secret can leave
  `.env` without the app changing again.
- Values are text up to 8192 characters, held in memory only (container
  reads are cached for 30 s); the SDK never writes or logs them.
- An app can still leak a secret it has been given; the store only keeps
  apps from reading each other's.

`FakeBroker(app_id="cameras")` answers `secrets.*` from `.secrets`
(`{(app, name): value}`), keyed by its own app the way the hub keys them by
token.

## Units

The hub has one units setting, metric or imperial, for every app: the °C/°F
switch in its title bar (a site sets the starting value with
`[apps.hub] units` in its `site.toml`). Apps read it and show their numbers
in it; they can't change it. An app may offer its own view-only switch by
passing the choice to `units()`.

```python
from bdh_sdk.units import system, units

system()              # "metric" or "imperial"
u = units()           # or units(request.GET.get("units")) for a view-only switch
u.temp_text(15.6)     # "16°C" / "60°F"
u.speed_text(10)      # "10 km/h" / "6 mph"
u.length_text(1000)   # "1,000 m" / "3,281 ft"
u.t(15.6), u.temp_unit  # 16, "°C"
```
## Location

The hub has one location, set by an admin under Settings > General: a city
picked from a search, and its time zone (taken from the city unless the admin
picks another). Apps read it; they can't change it.

```python
from bdh_sdk.location import location, tzinfo

where = location()        # None until the hub's location is set
where.label               # "Sun Peaks, British Columbia, Canada"
where.name                # "Sun Peaks"
where.latitude, where.longitude  # 50.88, -119.91 (None if unknown)
where.timezone            # "America/Vancouver"
tzinfo()                  # ZoneInfo("America/Vancouver"); UTC when unset
```
## Branding

`bdh_sdk.branding.brand()` returns the site's `name`, `subtitle` and
`monogram` / `monogram_light` SVG paths; `monogram_svg(light=False)` returns
the SVG text (100x100 viewBox; `light` is the version for dark backgrounds).
The hub sets them per site through `settings.HUB_BRAND`. Anything a site
leaves out falls back to "Black Diamond Hub" and the BDH monogram shipped
with the SDK.

## QR codes

`bdh_sdk.qr.qr_svg(text)` returns an SVG QR code in the hub style: round
dots, square finder patterns and the site's monogram in the centre (error
correction H keeps it scannable). Text over 200 characters drops to error
correction L with a smaller monogram so the code isn't too dense to scan.
The logo hole is sized from the actual codeword layout so it never uses more
than 40% of any error-correction block (small codes get a smaller logo, or
none). Pass `logo=<svg text>` to use a different centre image. Install the
`bdh-sdk[qr]` extra; the hub already ships `qrcode`.

```python
from bdh_sdk.qr import qr_svg

svg = qr_svg("http://192.168.10.216/settings/display/upload/abc/")
```

## Alerts

`bdh_sdk.alerts.push(Alert(...))` pops a modal up on every hub screen, in
every mode and on every page, waking the screensaver. It shows `title`, an optional
`body` and an `<iframe>` of `embed_url`, which must be a path on the hub. The modal closes
after `ttl` seconds (1-600, default 60), or when someone taps X or the
backdrop. Pushing again with the same `key` while it's showing restarts the timer;
a different alert replaces it. Nothing goes to phones.

A new alert plays a chime. `sound=None` (the default) uses the hub's default
chime, `"none"` (`SILENT`) is quiet, a name from `CHIMES` picks a built-in
one (`"ding-dong"`, `"soft-bell"`, `"notify"`, `"knock-knock"`, ...), and a
path on the hub (e.g. `/static/myapp/ring.mp3`) plays your own file. Repeats
with the same `key` don't chime again unless `resound=True`, and nothing plays
during the hub's night window. Settings pages can preview a chime with
`BDHChimes.play(name)`.

`speak="Egg timer has expired"` (up to 300 characters) has the screens read
the text out after the chime, using Home Assistant's text-to-speech (see
`speakers.tts_audio`). `dismiss(app, key)` closes the alert on every screen,
for example when it was dealt with on another one.

```python
from bdh_sdk.alerts import Alert, push

push(Alert(app="cameras", key="doorbell:front", title="Someone's at the door",
           embed_url="/cameras/popup/front/", ttl=60, sound="ding-dong"))
```

`push` never blocks and is safe from any thread, including one running its own
event loop. Async code can `await apush(alert)`. The hub installs the delivery
function with `set_sender`. Without one (unit tests, scripts), pushing only
logs at debug level. Import `bdh_sdk.alerts` inside your worker, not in `plugin.py`.

## Events

`bdh_sdk.events` lets apps react to things happening elsewhere without talking
to Home Assistant (or anything else) directly. The hub runs event sources,
declared per site in `site.toml`, and publishes what they see; apps subscribe.

```python
from bdh_sdk import events

def door(event):  # event.topic, event.data, event.time, event.source
    if event.data["new"] == "on" and event.data["old"] == "off":
        ...

events.subscribe("ha/state/binary_sensor.front_door", door)
events.publish("cameras/doorbell/front", {"camera": "front"})  # your own events
```

- Topics are `/`-separated. The first segment is the source name from
  `site.toml` (e.g. `ha`, `sunpeaks`, `hook`) or the publishing app's id; apps
  publish under their own id.
- Home Assistant sources publish `<source>/state/<entity_id>` with data
  `{"entity_id", "old", "new", "attributes"}`. They only fetch entities
  someone subscribed to, so subscribe to exact entities where you can.
- Patterns take MQTT wildcards: `+` is one segment, a trailing `#` is the rest.
- Handlers run on one shared dispatcher thread and must not block. Plain
  functions are called directly; `async def` handlers run as tasks there. An
  exception is logged and doesn't affect other handlers.
- Delivery is best effort: nothing is replayed after a source reconnects.
- In a container app, the SDK sends your subscriptions to the hub, retrying
  until the hub has them if it can't be reached; the hub keeps them across
  its own restarts.
- `subscribe` returns a `Subscription` with `cancel()`. Subscribe from your
  `AppConfig.ready()` (or your worker), never at `plugin.py` import time.
- `publish` is thread-safe and never blocks; async code can
  `await events.apublish(...)`.
- Tests: `events.flush()` waits for pending handlers and `events.reset()`
  drops all subscriptions.

## Admin check

`is_admin(request)` is true when a signed-in, active hub admin (Django staff
user) made the request. Use it instead of reading `request.user` fields, so
every app follows the hub's rule and never touches the user table.

```python
from bdh_sdk import is_admin

if not is_admin(request):
    return HttpResponseForbidden()
```

## Home Assistant client

`bdh_sdk.ha` talks to Home Assistant for apps that need it (install the
`bdh-sdk[ha]` extra; the hub already ships `requests` and `websocket-client`).
A hub can have several Home Assistant instances, typically one per site. Every
call takes an optional `instance` id; without it the call goes to the default
instance. `ha.instances()` lists them as dicts with `id`, `name`, `site` and
`default`. The hub registers its instances with `ha.set_provider()`; without a
provider there is one instance, `ha`, whose connection comes from
`HOMEASSISTANT_URL` and `HOMEASSISTANT_ACCESS_TOKEN` (Django settings, then the
environment).

```python
from bdh_sdk import ha

state = ha.get_entity_state("light.kitchen")       # dict or None
ok, err = ha.call_service("light", "turn_on", "light.kitchen")
for inst in ha.instances():                          # [{"id": "mi", ...}, ...]
    states = ha.get_all_states(instance=inst["id"])
```

`get_registry(platform=None)` returns the entity and device registries trimmed
to what apps need: `{"entities": [...], "devices": [...], "unit_system": {...}}`.
Entities have `entity_id`, `device_id`, `platform`, `translation_key`,
`disabled_by`, `name` and `original_name`; `platform="napoleon"` keeps just that
integration's. Devices are only those the entities belong to (`id`, `name`,
`name_by_user`, `manufacturer`, `model`). In a container the broker answers it
(op `ha.registry`, needs any `ha.read` grant) with only the entities the app may
read, so apps don't need the admin-only `ha.ws` grant that `ws_commands` does.

Read helpers (`get_entity_state`, `get_entity_states`, `get_history`) return
`None`/`{}` when HA is unreachable, so pages can still render.
`get_all_states`, `get_registry`, `get_static`, `upload_media` and `ws_commands`
raise `ha.HAError` instead.

## Speakers

`bdh_sdk.speakers` makes announcements on the house speakers, so an app can
talk without knowing Home Assistant (needs the `bdh-sdk[ha]` extra and an
admin HA token, used to upload the chimes to HA's local media).

```python
from bdh_sdk import Setting
from bdh_sdk.speakers import announce

Setting("speakers", "Speakers", "multi", default=[],
        choices_from="bdh_sdk.speakers.list_speakers")

failed = announce("Dinner's ready", ["media_player.kitchen"], volume=40,
                  chime="ding-dong", voice="AvaNeural")
```

`list_speakers()` lists Sonos and Alexa (`alexa_media`) speakers as
`(entity_id, "Kitchen (Sonos)")` pairs. `announce()` plays the chime (one of
`alerts.CHIMES`) and then the message on Sonos, ducking and resuming the music,
and sends Alexa devices an announcement, which has its own tone. It blocks
until done, returns the speakers that failed and raises
`speakers.AnnounceError` if all of them did. Announcements never overlap.

`tts_audio(message, voice=None)` returns `(audio bytes, content type)` of HA
speaking the message (at most 300 characters), cached for repeats. The hub uses
it to speak alerts on its own screens (`Alert.speak`); it raises `ha.HAError`
when HA can't make it.

## Testing helpers

```python
from bdh_sdk.testing import assert_plugin_importable

assert_plugin_importable("wifi.plugin")
```

Imports the module in a fresh interpreter and fails if it doesn't expose a
`PluginSpec` named `spec`, sets up Django or configures settings, starts
threads, or imports `requests`, any `*.models` module, or the app's `views`
or `homepage` modules. Pass `forbidden=("name", ...)` to ban more modules.

## Running in a container

With the `runtime` extra, an app runs on its own: `python -m bdh_sdk.runtime`
serves the app named by `BDH_APP_ID` with uvicorn, a SQLite database in
`BDH_DATA_DIR` and the SDK's own `base.html`. The hub's supervisor starts it
and the hub proxies to it inside an iframe.

- `BDH_BROKER_URL` / `BDH_APP_TOKEN` switch settings, state, secrets, alerts,
  events, speakers and `ha` to RPC calls to the hub broker (`bdh_sdk.rpc`). Without
  them everything runs in-process as before.
- `bdh_sdk.identity.IdentityMiddleware` only accepts requests the hub signed
  with the app token and sets `request.user` (`username`, `is_staff`) and
  `request.bdh_mode` from the signed identity header.
- The hub reads tiles, status and shortcuts through `POST /_bdh/hook` and
  relays events through `POST /_bdh/events`; `GET /_bdh/health` is unsigned.
- `bdh/embed.js` talks to the hub shell with `postMessage` (title, resize,
  activity, navigate, toast, theme, open-overlay). `theme` sends the page's
  background colour so the hub paints it behind the frame (no white flash).
  `bdh.stats(fn)` adds the object `fn()` returns to the hub's remote
  `frame state` (development only). While the hub's remote is on it can drive
  the app's frame: the first `remote` message loads `bdh/embed-remote.js`, which
  runs a fixed set of commands (`state`, `query`, `tap`, `swipe`, `key`,
  `measure`, `logs`; no eval) and stands in for fullscreen, which synthetic taps
  can't request.

Manifest fields for the app store: `permissions`, `network`
(`none`/`outbound`/`lan`), `lifecycle` (`always`/`on_demand`), `memory_mb`,
`companions`, `wake_every`. An `on_demand` app is stopped after 10 idle
minutes; while it sleeps the hub shows its last status, card and stats and
only wakes it when they are older than `wake_every` seconds (0 = the hub's
default, 6 hours). Apps that must update sooner should use `always`.

`bdh_sdk.testing.FakeBroker` is an in-process broker for app tests: register
handlers with `.on(op, fn)` and check `.calls`. It answers `ha.registry` from
`.registry`.

`python -m bdh_sdk.runtime.manage <command>` runs Django management commands
in the container (without starting the app's server or background workers).

### Moving an app's rows into its container

An app that used to run in the hub keeps its rows in the hub's database.
`bdh_sdk.appdata` moves them as a Django JSON fixture (one app label only):

```sh
# in the hub (its own DJANGO_SETTINGS_MODULE)
python -m bdh_sdk.appdata export cameras -o cameras.json
# copy cameras.json into the app's data volume, then in the container:
python -m bdh_sdk.runtime.manage import_app_data /data/cameras.json [--replace]
```

`export_app_data [label] [-o file]` is the container-side twin, and
`python -m bdh_sdk.appdata import FILE` migrates a fresh container before
loading. Imports run in one transaction, refuse rows of other apps and reset
sequences; `--replace` deletes the app's existing rows first. Both directions
stream one row at a time, so a fixture full of blobs (photos) imports within
the app's normal memory limit.

## Reusable CI for app repos

`.github/workflows/app-ci.yml` is a reusable workflow. In an app repo:

```yaml
# .github/workflows/ci.yml
name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  hub:
    uses: blackdiamondlodge/bdh-sdk/.github/workflows/app-ci.yml@v0
    with:
      app_id: wifi
```

It checks out the app (into `app/`) and `blackdiamondlodge/BlackDiamondHub`
(into `hub/`), installs the hub requirements and `bdh-sdk`, builds and
installs the app wheel, then:

- runs `assert_plugin_importable("<app_id>.plugin")` against the installed wheel;
- runs `manage.py check` and `manage.py collectstatic --noinput` with
  `DJANGO_SETTINGS_MODULE=BlackDiamondHub.settings_test`;
- runs `manage.py hub_smoke --app <app_id>` (plugin loaded via entry point,
  tile URLs reverse and return < 500 for a superuser, icon resolves);
- runs `manage.py test <test_module> --exclude-tag=selenium`;
- in a second job (input `selenium`, default true), runs the app's tests
  tagged `selenium` in the runner's headless Chrome (with a go2rtc server
  on `localhost:1984` when `go2rtc: true`).

- in a third job (input `image`, default true), builds the app's container
  image the way the platform will and smoke-tests it like the supervisor
  runs it (`builder/app_artifacts.sh`; see [builder/README.md](builder/README.md)).

Inputs: `app_id` (required), `hub_ref` (default `main`), `python-version`
(default `3.12`), `test_module` (default `app_id`), `sdk_ref` (default
`v0`), `selenium` (default true), `go2rtc` (default false), `image`
(default true), `hub_api` (default 2), `smoke_env` (extra `KEY=VALUE` lines
for the image smoke test), `smoke_settle` (default 5 seconds). Optional
secret `hub_token` if the hub repo is private.

`.github/workflows/app-release.yml` is the matching reusable release
workflow. Call it on pushes to `main`:

```yaml
jobs:
  release:
    uses: blackdiamondlodge/bdh-sdk/.github/workflows/app-release.yml@v0
    permissions:
      contents: write
      id-token: write      # build provenance attestation
      attestations: write
```

The first time a `pyproject.toml` version lands on main, the workflow tags
`v<version>` and does the following:

1. builds the wheel;
2. locks the app's third-party dependencies against the current
   `bdh-app-runtime` base (`bdh-lock.txt`);
3. builds and smoke-tests the image;
4. attests the assets;
5. publishes a GitHub release with the wheel, sdist, `bdh-lock.txt` and
   `bdh-app.json`.

App repos build no images and need no secrets. The platform builder in
[bdh-app-catalog](https://github.com/blackdiamondlodge/bdh-app-catalog)
turns the release into `ghcr.io/blackdiamondlodge/bdh-app-<id>` and opens the
catalog PR. Declare system packages, excluded dependencies and `custom_base`
in `[tool.bdh]`; see [builder/README.md](builder/README.md).
## Development

```
python -m unittest discover -s tests -v
python -m build
```

## Releasing

Bump `version` in `pyproject.toml` and `bdh_sdk/__init__.py` and merge to
`main`. `release.yml` tags `v<version>`, moves the major tag (e.g. `v0`),
and creates a GitHub release with the wheel and sdist attached. If the tag
already exists the workflow does nothing.

## License

MIT
