"""Turns an app's hook results into the JSON the hub renders.

The hub used to call ``PluginSpec`` hooks in-process and convert the
dataclasses itself. An app in its own container runs its hooks here
instead, and the runtime returns the JSON over its signed ``/_bdh/hook``
endpoint. :func:`run` is the single entry point.

Image and card URLs are paths on the app's own origin; the hub rewrites
them before showing them.
"""
import inspect
import logging
import re
import time
from datetime import timezone
from importlib import import_module

from .plugins import STATUS_LEVELS, INFO, Action, Card, Reading, Shortcut, Status, StatusRow, Stay
from .provides import (
    BACKGROUNDS, LIVE, MAX_SAVER_IMAGES, SAVER, SAVER_TEXT_MAX, SEASON, STATS, Background,
    SaverImage, SeasonHint, Stat,
)

__all__ = ["HOOKS", "HookNotFound", "run"]

log = logging.getLogger(__name__)

# Fastest a status may ask the homepage to poll (Status.refresh).
MIN_REFRESH = 2

_SLUG = re.compile(r"[-a-zA-Z0-9_]+")

LEFT = "{left}"


def fmt_left(secs):
    """``4:05`` / ``1:02:03``, rounded up so ``0:00`` only shows once over."""
    secs = max(0, int(-(-secs // 1)))
    h, rest = divmod(secs, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def countdown(key, text, ends_at, now=None):
    """JSON for a ``text`` that may count down to ``ends_at``.

    Always has ``key`` with ``{left}`` filled in for now. With a countdown it
    also has ``<key>_tpl`` and ``ends_at`` so the screen can tick it itself.
    """
    if ends_at is None or LEFT not in text:
        return {key: text}
    now = time.time() if now is None else now
    return {key: text.replace(LEFT, fmt_left(ends_at - now)),
            f"{key}_tpl": text, "ends_at": float(ends_at)}


class HookNotFound(LookupError):
    """The app has no such hook."""


def _resolve(path):
    module_path, _, attr = path.rpartition(".")
    return getattr(import_module(module_path), attr)


def _takes_mode(fn):
    try:
        return "mode" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _image_url(url):
    """``url`` if the screen's browser may load it as an image, else ""."""
    url = str(url or "")
    return url if re.match(r"(https?://|/(?!/))", url) else ""


def _actions_value(actions):
    items = []
    for a in actions:
        if not isinstance(a, Action):
            raise TypeError(f"status action is {type(a).__name__}")
        if not _SLUG.fullmatch(a.id):
            log.warning("Skipping status action with invalid id %r", a.id)
            continue
        items.append({"id": a.id, "icon": a.icon, "label": a.label,
                      "primary": bool(a.primary)})
    return items


def readings_value(readings, what):
    items = []
    for r in readings:
        if not isinstance(r, Reading):
            raise TypeError(f"{what} reading is {type(r).__name__}")
        items.append({"label": r.label, "value": r.value, "unit": r.unit})
    return items


def status_value(result):
    if result is None:
        return None
    if isinstance(result, str):
        result = Status(result)
    if not isinstance(result, Status):
        raise TypeError(f"status hook returned {type(result).__name__}")
    level = result.level if result.level in STATUS_LEVELS else INFO
    now = time.time()
    value = {**countdown("text", result.text, result.ends_at, now), "level": level}
    rows = []
    for row in result.rows:
        if not isinstance(row, StatusRow):
            raise TypeError(f"status row is {type(row).__name__}")
        if not _SLUG.fullmatch(row.id):
            log.warning("Skipping status row with invalid id %r", row.id)
            continue
        rows.append({
            "id": row.id,
            "title": row.title,
            **countdown("detail", row.detail, row.ends_at, now),
            "image": _image_url(row.image),
            "dimmed": bool(row.dimmed),
            "actions": _actions_value(row.actions),
        })
    actions = _actions_value(result.actions)
    readings = readings_value(result.readings, "status")
    if rows:
        value["rows"] = rows
    if actions:
        value["actions"] = actions
    if readings:
        value["readings"] = readings
    if result.refresh:
        value["refresh"] = max(MIN_REFRESH, int(result.refresh))
    return value


def shortcuts_value(result):
    items = []
    for sc in result or ():
        if not isinstance(sc, Shortcut):
            raise TypeError(f"shortcuts hook returned {type(sc).__name__}")
        if not _SLUG.fullmatch(sc.id):
            log.warning("Skipping shortcut with invalid id %r", sc.id)
            continue
        items.append({"id": sc.id, "title": sc.title, "icon": sc.icon,
                      "order": sc.order, "modes": sorted(sc.modes)})
    return items


def stay_value(result):
    if result is None:
        return None
    if not isinstance(result, Stay):
        raise TypeError(f"stay hook returned {type(result).__name__}")
    return {"guest": result.guest, "arrival": result.arrival,
            "checkout": result.checkout, "owner": bool(result.owner)}


def card_value(result):
    if result is None:
        return None
    if not isinstance(result, Card):
        raise TypeError(f"card hook returned {type(result).__name__}")
    url = None
    if result.url_name:
        from django.urls import NoReverseMatch, reverse
        try:
            url = reverse(result.url_name)
        except NoReverseMatch:
            log.warning("Card links to unknown URL name %r", result.url_name)
    return {
        "title": result.title,
        "detail": result.detail,
        "readings": readings_value(result.readings, "card"),
        "alerts": [str(a) for a in result.alerts],
        "url": url,
        "level": result.level if result.level in STATUS_LEVELS else INFO,
    }


def _iso(when):
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def stats_value(allowed, result):
    out = {}
    for stat in list(result or ()):
        if not isinstance(stat, Stat):
            raise TypeError(f"stats provider returned {type(stat).__name__}")
        if stat.key not in allowed:
            log.warning("Skipping undeclared stat %r", stat.key)
            continue
        if stat.key in out:
            continue
        item = {"label": stat.label, "order": stat.order}
        if stat.time is not None:
            item["time"] = _iso(stat.time)
        else:
            item["value"] = str(stat.value)
        out[stat.key] = item
    return out


def backgrounds_value(result):
    out, seen = [], set()
    for bg in list(result or ()):
        if not isinstance(bg, Background):
            raise TypeError(f"backgrounds provider returned {type(bg).__name__}")
        if bg.id in seen or not bg.image_url:
            continue
        seen.add(bg.id)
        out.append({"id": bg.id, "label": bg.label, "url": bg.image_url,
                    "refresh_seconds": bg.refresh_seconds, "live": bg.live})
    return out


def saver_value(sources, result):
    """Validated pictures for the declared ``sources`` (:class:`SaverSource`)."""
    kinds = {src.key: src.kind for src in sources}
    out, seen, counts = [], set(), {}
    for img in list(result or ()):
        if not isinstance(img, SaverImage):
            raise TypeError(f"saver provider returned {type(img).__name__}")
        kind = kinds.get(img.source)
        if kind is None:
            log.warning("Skipping saver image for undeclared source %r", img.source)
            continue
        if kind == LIVE and img.refresh_seconds is None:
            log.warning("Skipping live saver image %r without refresh_seconds", img.id)
            continue
        if (img.source, img.id) in seen or len(img.id) > SAVER_TEXT_MAX:
            continue
        if counts.get(img.source, 0) >= MAX_SAVER_IMAGES:
            continue
        seen.add((img.source, img.id))
        counts[img.source] = counts.get(img.source, 0) + 1
        item = {"source": img.source, "id": img.id, "url": img.image_url,
                "label": img.label[:SAVER_TEXT_MAX], "caption": img.caption[:SAVER_TEXT_MAX]}
        if kind == LIVE:
            item["refresh_seconds"] = img.refresh_seconds
        out.append(item)
    return out


def season_value(result):
    if result is None:
        return None
    if not isinstance(result, SeasonHint):
        raise TypeError(f"season provider returned {type(result).__name__}")
    value = {"season": result.season, "reason": result.reason}
    if result.opens_on is not None:
        value["opens_on"] = result.opens_on.isoformat()
    return value


def _message(result):
    return None if result is None else str(result)


HOOKS = ("status", "shortcuts", "stay", "card", "run_shortcut", "run_action",
         "provide", "setting_options", "tools", "run_tool")


def tools_value(result):
    from .tools import Tool, tool_dict

    items, seen = [], set()
    for tool in result or ():
        if not isinstance(tool, Tool):
            raise TypeError(f"tools hook returned {type(tool).__name__}")
        if tool.id in seen:
            log.warning("Skipping duplicate tool id %r", tool.id)
            continue
        seen.add(tool.id)
        items.append(tool_dict(tool))
    return items


def tool_result(result):
    """``result`` as JSON for the model; raises TypeError if it can't be."""
    import json

    if result is None or isinstance(result, str):
        return result
    try:
        return json.loads(json.dumps(result))
    except (TypeError, ValueError):
        raise TypeError(f"run_tool returned {type(result).__name__}, not JSON") from None


def run(spec, hook, *, mode=None, shortcut_id=None, row_id="", action_id=None,
        capability=None, key=None, tool_id=None, args=None):
    """Run ``spec``'s ``hook`` and return its JSON-ready result.

    Raises :class:`HookNotFound` when the app doesn't have it; anything the
    app raises propagates.
    """
    if hook in ("status", "shortcuts", "stay", "card", "tools"):
        path = getattr(spec, hook)
        if not path:
            raise HookNotFound(hook)
        fn = _resolve(path)
        result = fn(mode=mode) if mode and _takes_mode(fn) else fn()
        return {"status": status_value, "shortcuts": shortcuts_value,
                "stay": stay_value, "card": card_value, "tools": tools_value}[hook](result)
    if hook == "run_tool":
        if not spec.run_tool:
            raise HookNotFound(hook)
        fn = _resolve(spec.run_tool)
        args = dict(args or {})
        if _takes_mode(fn):
            return tool_result(fn(tool_id, args, mode=mode))
        return tool_result(fn(tool_id, args))
    if hook == "run_shortcut":
        if not spec.run_shortcut:
            raise HookNotFound(hook)
        return _message(_resolve(spec.run_shortcut)(shortcut_id))
    if hook == "run_action":
        if not spec.run_action:
            raise HookNotFound(hook)
        fn = _resolve(spec.run_action)
        if _takes_mode(fn):
            return _message(fn(row_id, action_id, mode=mode))
        return _message(fn(row_id, action_id))
    if hook == "provide":
        path = spec.provides.get(capability)
        if not path:
            raise HookNotFound(f"provide:{capability}")
        result = _resolve(path)()
        if capability == STATS:
            return stats_value({k for k, _label in spec.stat_keys}, result)
        if capability == BACKGROUNDS:
            return backgrounds_value(result)
        if capability == SEASON:
            return season_value(result)
        if capability == SAVER:
            return saver_value(spec.saver_sources, result)
        raise HookNotFound(f"provide:{capability}")
    if hook == "setting_options":
        setting = next((s for s in spec.settings if s.key == key), None)
        if setting is None:
            raise HookNotFound(f"setting:{key}")
        return [[value, label] for value, label in setting.options]
    raise HookNotFound(hook)
