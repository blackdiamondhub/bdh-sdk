"""Tools: things the hub's assistant can look up or do through an app.

An app declares :class:`Tool` objects with its ``PluginSpec.tools`` hook and
runs them with ``run_tool``. The hub lists them to an assistant (a language
model behind a voice app, say) and checks every call: the tool must be
offered in the session's mode, and its arguments must match ``params``
(:func:`validate_args`) before the app is called.

An app granted the ``tools`` permission can run an assistant session:
:func:`open_session` gets a short-lived token for one conversation,
:func:`list_tools` and :func:`call_tool` use it. The hub, not the app, picks
the session's mode and records every call as an ``assistant/<sid>/...``
event.

Nothing here imports Django.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

from .plugins import ALL_MODES, OWNER_ONLY

__all__ = [
    "InvalidArgs", "Tool", "ToolError", "call_tool", "check_params", "list_tools", "open_session",
    "tool_dict", "tool_name", "validate_args",
]

TOOL_ID_RE = re.compile(r"[a-z0-9]+(_[a-z0-9]+)*")
MAX_TOOL_ID = 40
PARAM_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,39}")
# OpenAI's rule for function names.
NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
SEP = "__"
MAX_TITLE = 60
MAX_DESCRIPTION = 300
MAX_PARAMS = 20
MAX_ENUM = 50
TYPES = ("string", "integer", "number", "boolean")
_PROP_KEYS = frozenset({"type", "description", "enum", "minimum", "maximum", "minLength", "maxLength"})
_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})

# Session kinds an app may open. "voice" sessions are never owner mode.
VOICE = "voice"
KINDS = frozenset({VOICE})
# BrokerError code for a token that is forged, expired or another app's:
# open a new session.
BAD_SESSION = "bad_session"
SESSION_SECONDS = 15 * 60


class InvalidArgs(ValueError):
    """A tool call's arguments don't match the tool's ``params``."""


class ToolError(Exception):
    """Raise from ``run_tool`` with a message the assistant may pass on.

    Other exceptions are reported to the assistant as a bare failure.
    """


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_prop(name, prop):
    if not isinstance(name, str) or not PARAM_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid parameter name {name!r}")
    if not isinstance(prop, dict):
        raise ValueError(f"parameter {name!r} must be a dict")
    extra = set(prop) - _PROP_KEYS
    if extra:
        raise ValueError(f"parameter {name!r} has unsupported keys {sorted(extra)}")
    kind = prop.get("type")
    if kind not in TYPES:
        raise ValueError(f"parameter {name!r} type must be one of {list(TYPES)}")
    out = {"type": kind}
    if "description" in prop:
        if not isinstance(prop["description"], str):
            raise ValueError(f"parameter {name!r} description must be a string")
        out["description"] = prop["description"]
    if "enum" in prop:
        enum = prop["enum"]
        if not isinstance(enum, (list, tuple)) or not 0 < len(enum) <= MAX_ENUM:
            raise ValueError(f"parameter {name!r} enum must be a list of 1-{MAX_ENUM} values")
        for value in enum:
            try:
                _coerce(kind, value)
            except InvalidArgs:
                raise ValueError(f"parameter {name!r} enum value {value!r} isn't a {kind}") from None
        out["enum"] = list(enum)
    for key in ("minimum", "maximum"):
        if key in prop:
            if kind not in ("integer", "number") or not _is_number(prop[key]):
                raise ValueError(f"parameter {name!r} {key} needs a numeric type and value")
            out[key] = prop[key]
    for key in ("minLength", "maxLength"):
        if key in prop:
            value = prop[key]
            if kind != "string" or not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"parameter {name!r} {key} needs a string type and a count")
            out[key] = value
    return out


def check_params(params):
    """``params`` as a normalised JSON-schema object; raises ValueError.

    Only a flat object is supported: properties of type string, integer,
    number or boolean, with optional ``description``, ``enum``,
    ``minimum``/``maximum`` and ``minLength``/``maxLength``. Unknown
    arguments are refused (``additionalProperties`` is always false).
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON-schema object (a dict)")
    extra = set(params) - _SCHEMA_KEYS
    if extra:
        raise ValueError(f"params has unsupported keys {sorted(extra)}")
    if params.get("type", "object") != "object":
        raise ValueError('params type must be "object"')
    if params.get("additionalProperties", False) is not False:
        raise ValueError("params additionalProperties must be false")
    props = params.get("properties") or {}
    if not isinstance(props, dict):
        raise ValueError("params properties must be a dict")
    if len(props) > MAX_PARAMS:
        raise ValueError(f"a tool takes at most {MAX_PARAMS} parameters")
    properties = {name: _check_prop(name, prop) for name, prop in props.items()}
    required = params.get("required") or []
    if (not isinstance(required, (list, tuple)) or len(set(required)) != len(required)
            or any(r not in properties for r in required)):
        raise ValueError("params required must list each declared parameter at most once")
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


@dataclass(frozen=True)
class Tool:
    """Something the assistant can look up or do through the app.

    ``id`` is lowercase letters, digits and single underscores (at most 40
    chars), unique within the app. ``description`` tells the model when to use
    the tool (at most 300 chars); ``params`` is a flat JSON-schema object
    (see :func:`check_params`). ``modes`` are the homepage modes whose
    assistant sessions may use the tool: owner-only unless the app lists
    others, since guest and dashboard sessions don't require sign-in.
    ``readonly`` marks a pure lookup; ``confirm`` asks the assistant to
    confirm with the user before running it.
    """

    id: str
    title: str
    description: str
    params: dict = field(default_factory=dict, hash=False)
    modes: frozenset = OWNER_ONLY
    confirm: bool = False
    readonly: bool = False

    def __post_init__(self):
        if (not isinstance(self.id, str) or len(self.id) > MAX_TOOL_ID
                or not TOOL_ID_RE.fullmatch(self.id)):
            raise ValueError(f"invalid tool id {self.id!r}")
        if not isinstance(self.title, str) or not 0 < len(self.title.strip()) <= MAX_TITLE:
            raise ValueError(f"tool {self.id!r} title must be 1-{MAX_TITLE} chars")
        if not isinstance(self.description, str) or not 0 < len(self.description.strip()) <= MAX_DESCRIPTION:
            raise ValueError(f"tool {self.id!r} description must be 1-{MAX_DESCRIPTION} chars")
        try:
            params = check_params(self.params)
        except ValueError as exc:
            raise ValueError(f"tool {self.id!r}: {exc}") from None
        if isinstance(self.modes, str):
            raise ValueError(f"tool {self.id!r} modes must be a set of modes")
        modes = frozenset(self.modes)
        if not modes or not modes <= ALL_MODES:
            raise ValueError(f"tool {self.id!r} modes must be a non-empty subset of {sorted(ALL_MODES)}")
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "confirm", bool(self.confirm))
        object.__setattr__(self, "readonly", bool(self.readonly))


def tool_dict(tool):
    """``tool`` as the JSON the runtime sends the hub."""
    return {"id": tool.id, "title": tool.title, "description": tool.description,
            "params": tool.params, "modes": sorted(tool.modes), "confirm": tool.confirm,
            "readonly": tool.readonly}


def _coerce(kind, value):
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "boolean" and isinstance(value, bool):
        return value
    if kind == "integer" and _is_number(value):
        if isinstance(value, int):
            return value
        if value.is_integer():
            return int(value)
    if kind == "number" and _is_number(value):
        return value
    raise InvalidArgs(f"must be a{'n' if kind[0] in 'ai' else ''} {kind}")


def validate_args(tool, args):
    """The cleaned arguments for calling ``tool``; raises :class:`InvalidArgs`.

    ``tool`` is a :class:`Tool` or its ``params`` schema. ``None`` values
    count as absent. Integral floats become ints for integer parameters.
    """
    schema = tool.params if isinstance(tool, Tool) else check_params(tool)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise InvalidArgs("arguments must be an object")
    props = schema["properties"]
    unknown = sorted(str(k) for k in args if k not in props)
    if unknown:
        raise InvalidArgs(f"unknown argument {unknown[0]!r}")
    cleaned = {}
    for name, value in args.items():
        if value is None:
            continue
        prop = props[name]
        try:
            value = _coerce(prop["type"], value)
        except InvalidArgs as exc:
            raise InvalidArgs(f"{name} {exc}") from None
        if "enum" in prop and value not in prop["enum"]:
            raise InvalidArgs(f"{name} must be one of {', '.join(map(str, prop['enum']))}")
        if "minimum" in prop and value < prop["minimum"]:
            raise InvalidArgs(f"{name} must be at least {prop['minimum']}")
        if "maximum" in prop and value > prop["maximum"]:
            raise InvalidArgs(f"{name} must be at most {prop['maximum']}")
        if "minLength" in prop and len(value) < prop["minLength"]:
            raise InvalidArgs(f"{name} must be at least {prop['minLength']} characters")
        if "maxLength" in prop and len(value) > prop["maxLength"]:
            raise InvalidArgs(f"{name} must be at most {prop['maxLength']} characters")
        cleaned[name] = value
    missing = [r for r in schema["required"] if r not in cleaned]
    if missing:
        raise InvalidArgs(f"missing required argument {missing[0]!r}")
    return cleaned


def tool_name(*parts):
    """A model-safe function name (``^[a-zA-Z0-9_-]{1,64}$``) for ``parts``.

    Parts are joined with ``__``. A name that needed characters replaced, or
    was too long, ends in a short hash of the parts so it stays unique.
    """
    raw = SEP.join(str(p) for p in parts)
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    if clean == raw and NAME_RE.fullmatch(raw):
        return raw
    digest = hashlib.sha1(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:8]
    return f"{clean[:55]}_{digest}"


def open_session(kind=VOICE, display=None):
    """Start an assistant session and return its token (valid ~15 minutes).

    ``display`` is the IP of the screen the conversation happens on, if any;
    it is recorded with every call. Needs the ``tools`` permission.
    """
    from . import rpc

    payload = {"kind": kind}
    if display:
        payload["display"] = str(display)
    return rpc.client().call("tools.open", payload)["token"]


def list_tools(token):
    """The tools the session may use, as dicts.

    Each has ``name`` (the function name to give the model), ``title``,
    ``description``, ``parameters`` (JSON schema), ``readonly`` and
    ``confirm``. The list can change during a session (a stay starts, an
    app goes away), so ask again rather than caching it for long.
    """
    from . import rpc

    return list(rpc.client().call("tools.list", {"token": token}).get("tools") or [])


def call_tool(token, name, args=None, *, timeout=30.0):
    """Run tool ``name``: ``{"ok": True, "result": ...}`` or ``{"ok": False, "error": str}``.

    A tool that fails, isn't offered, or gets bad arguments answers
    ``ok: false`` with a short reason for the model; a bad or expired token
    raises :class:`bdh_sdk.rpc.BrokerError`.
    """
    from . import rpc

    return rpc.client().call("tools.call", {"token": token, "name": name, "args": args or {}},
                             timeout=timeout)
