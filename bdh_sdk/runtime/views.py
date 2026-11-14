"""The runtime's own endpoints.

``/_bdh/`` paths are for the hub only: the identity middleware has already
checked the hub's signature over the method, path and body.
"""
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .. import hooks

log = logging.getLogger(__name__)

_HOOK_ARGS = ("mode", "shortcut_id", "row_id", "action_id", "capability", "key")


def _internal(request):
    return getattr(request, "bdh_internal", False)


def _json_body(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@csrf_exempt
@require_POST
def hook(request):
    """Run a PluginSpec hook for the hub: ``{"hook": ..., <args>}`` -> ``{"result": ...}``."""
    from . import app_spec

    if not _internal(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    body = _json_body(request)
    if body is None or not isinstance(body.get("hook"), str):
        return JsonResponse({"error": "bad request"}, status=400)
    kwargs = {k: body[k] for k in _HOOK_ARGS if body.get(k) is not None}
    try:
        result = hooks.run(app_spec(), body["hook"], **kwargs)
    except hooks.HookNotFound:
        return JsonResponse({"error": "no such hook"}, status=404)
    except Exception:
        log.exception("hook %s failed", body["hook"])
        return JsonResponse({"error": "hook failed"}, status=500)
    return JsonResponse({"result": result})


def _url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        log.warning("url %r does not reverse", name)
        return None


def describe(spec):
    """What the hub needs to show this app without importing it."""
    setting_fields = ("key", "label", "type", "default", "help", "minimum", "maximum", "unit")
    return {
        **spec.manifest(),
        "tiles": [
            {"title": t.title, "url": _url(t.url_name), "url_name": t.url_name, "icon": t.icon,
             "section": t.section, "subtitle": t.subtitle, "order": t.order,
             "modes": sorted(t.modes)}
            for t in spec.tiles
        ],
        "hooks": [h for h in ("status", "shortcuts", "run_shortcut", "run_action", "stay", "card")
                  if getattr(spec, h)],
        "settings": [
            {**{f: getattr(s, f) for f in setting_fields},
             "choices": [list(c) for c in s.options] if not s.choices_from else None,
             "dynamic_choices": bool(s.choices_from)}
            for s in spec.settings
        ],
        "settings_pages": [
            {"title": p.title, "url": _url(p.url_name), "subtitle": p.subtitle, "icon": p.icon}
            for p in spec.settings_pages
        ],
        "stat_keys": [list(k) for k in spec.stat_keys],
    }


@require_GET
def manifest(request):
    """``describe()`` of this app, for the hub."""
    from . import app_spec

    if not _internal(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    return JsonResponse(describe(app_spec()))


@csrf_exempt
@require_POST
def events(request):
    """Events the hub broker relays to this app: ``{"topic", "data", "time"}``."""
    from .. import events as bus

    if not _internal(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    body = _json_body(request)
    if body is None or not isinstance(body.get("topic"), str):
        return JsonResponse({"error": "bad request"}, status=400)
    try:
        bus.deliver(body["topic"], body.get("data"), body.get("time"))
    except ValueError:
        return JsonResponse({"error": "bad topic"}, status=400)
    return JsonResponse({"ok": True})


@require_GET
def tts(request):
    """``?text=`` spoken by Home Assistant (``chimes.js`` ``say()``)."""
    from .. import ha, speakers

    try:
        audio, content_type = speakers.tts_audio(
            request.GET.get("text", ""), voice=request.GET.get("voice") or None)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except ha.HAError:
        log.warning("text-to-speech failed", exc_info=True)
        return JsonResponse({"ok": False, "error": "text-to-speech unavailable"}, status=502)
    response = HttpResponse(audio, content_type=content_type)
    response["Cache-Control"] = "private, max-age=86400"
    return response


@require_GET
def hub_settings_app(request, app_id):
    """Back to the hub's settings page for ``app_id``: the shell does the navigating."""
    return render(request, "bdh/navigate.html", {
        "target": {"name": "hub_settings_app", "args": [app_id]},
    })
