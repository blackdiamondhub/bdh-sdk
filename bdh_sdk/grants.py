"""What an app may ask the broker for: the hub's grant rules.

Grants use the manifest permission grammar (``bdh_sdk.plugins.valid_permission``):
``ha.read:<entity glob>``, ``ha.call:<domain>.<service|*>``,
``events.publish:<topic pattern>``, ``events.subscribe:<topic pattern>``,
``alerts``, ``speakers``, ``media``, ``displays.read``, ``stays.read``, plus the admin-only ``ha.ws``.

The hub broker and :class:`bdh_sdk.testing.FakeBroker` both use these, so an
app's tests refuse exactly what the hub would.
"""
from fnmatch import fnmatchcase

from . import events

ALERTS = "alerts"
SPEAKERS = "speakers"
MEDIA = "media"
DISPLAYS_READ = "displays.read"
STAYS_READ = "stays.read"
HA_WS = "ha.ws"


def _with_prefix(grants, prefix):
    return [g[len(prefix):] for g in grants if g.startswith(prefix)]


def can_read(grants, entity_id):
    if not isinstance(entity_id, str) or not entity_id:
        return False
    return any(fnmatchcase(entity_id, glob) for glob in _with_prefix(grants, "ha.read:"))


def can_read_any(grants):
    return bool(_with_prefix(grants, "ha.read:"))


def can_call(grants, domain, service):
    if not isinstance(domain, str) or not isinstance(service, str):
        return False
    for grant in _with_prefix(grants, "ha.call:"):
        g_domain, _, g_service = grant.partition(".")
        if g_domain == domain and g_service in ("*", service):
            return True
    return False


def _own(app_id):
    return f"{app_id}/#"


def covers(granted, requested):
    """Whether topic pattern ``granted`` matches everything ``requested`` does."""
    try:
        want = events._segments(granted, wildcards=True)
        have = events._segments(requested, wildcards=True)
    except ValueError:
        return False
    for i, part in enumerate(want):
        if part == "#":
            return True
        if i >= len(have) or have[i] == "#":
            return False
        if part != "+" and (have[i] == "+" or part != have[i]):
            return False
    return len(want) == len(have)


def can_publish(grants, app_id, topic):
    try:
        events._segments(topic, wildcards=False)
    except ValueError:
        return False
    patterns = [_own(app_id), *_with_prefix(grants, "events.publish:")]
    return any(_safe_match(p, topic) for p in patterns)


def can_subscribe(grants, app_id, pattern):
    patterns = [_own(app_id), *_with_prefix(grants, "events.subscribe:")]
    return any(covers(p, pattern) for p in patterns)


def _safe_match(pattern, topic):
    try:
        return events.matches(pattern, topic)
    except ValueError:
        return False


def filter_registry(grants, reg, platform=None):
    """``reg`` (from :func:`bdh_sdk.ha.get_registry`) cut to what ``grants`` may read.

    Keeps readable entities (of ``platform``, if given), the devices they
    belong to, and the areas those entities or devices are in.
    """
    reg = reg or {}
    entities = [e for e in reg.get("entities") or () if isinstance(e, dict)
                and (platform is None or e.get("platform") == platform)
                and can_read(grants, e.get("entity_id"))]
    device_ids = {e.get("device_id") for e in entities if e.get("device_id")}
    devices = [d for d in reg.get("devices") or () if isinstance(d, dict) and d.get("id") in device_ids]
    area_ids = {x.get("area_id") for x in (*entities, *devices) if x.get("area_id")}
    areas = [a for a in reg.get("areas") or () if isinstance(a, dict) and a.get("area_id") in area_ids]
    return {"entities": entities, "devices": devices, "areas": areas,
            "unit_system": reg.get("unit_system") or {}}
