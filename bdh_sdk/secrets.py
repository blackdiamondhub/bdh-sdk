"""Per-app secrets: passwords, API keys and tokens an app needs at run time.

The hub keeps secrets encrypted (AES-256-GCM, key outside the database) and
each one belongs to a single app. An app reads and writes only its own:

    from bdh_sdk import secrets

    password = secrets.get("protect_password_1")
    secrets.set("refresh_token", new_token)   # e.g. a rotated OAuth token
    secrets.delete("refresh_token")

In a container (after :func:`bdh_sdk.rpc.install`) the calls go to the hub
broker, which works out the app from its token: the ``app`` argument is not
sent and can't reach another app's secrets. In-process (apps installed into
the hub itself) the hub installs a database backend with :func:`set_backend`
and the app passes its id: ``secrets.get("protect_password_1", app="cameras")``.
Without a backend, secrets live in memory (what unit tests get).

Values are text, kept in memory only; the SDK never writes them to disk or
logs them. A secret declared as a ``secret`` :class:`~bdh_sdk.plugins.Setting`
is entered on the hub's Settings page, which never shows it again.

:func:`get` falls back to the environment (``BDH_<APP>_<NAME>``, then any
``legacy`` names) when the store has no value, so a secret can move out of a
``.env`` without the app changing again.

This module does not import Django.
"""
import os
import re

__all__ = ["MAX_LENGTH", "MemorySecretBackend", "delete", "get", "set", "set_backend"]

#: The longest value the store accepts (characters).
MAX_LENGTH = 8192

_APP_ID = re.compile(r"^[a-z0-9_]+$")
_NAME = re.compile(r"^[a-z0-9_]{1,64}$")
_APP_ENV = "BDH_APP_ID"


def check_name(name):
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"invalid secret name: {name!r}")
    return name


def check_value(value):
    if not isinstance(value, str):
        raise TypeError("a secret must be a string")
    if not value:
        raise ValueError("a secret can't be empty; use delete()")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"a secret is at most {MAX_LENGTH} characters")
    return value


def _app(app):
    app = app or os.environ.get(_APP_ENV, "")
    if not app and getattr(_backend, "app_from_token", False):
        return ""
    if not isinstance(app, str) or not _APP_ID.match(app):
        raise ValueError("secrets need the app id: pass app=... (or set BDH_APP_ID)")
    return app


class MemorySecretBackend:
    """In-process backend: the default, and the one tests use."""

    def __init__(self):
        self._data = {}

    def get(self, app_id, name):
        return self._data[(app_id, name)]

    def set(self, app_id, name, value):
        self._data[(app_id, name)] = value

    def delete(self, app_id, name):
        self._data.pop((app_id, name), None)


_backend = MemorySecretBackend()


def set_backend(backend):
    """Install the backend behind this module; ``None`` restores memory.

    A backend has ``get(app_id, name)`` (raises ``KeyError`` when unset),
    ``set(app_id, name, value)`` and ``delete(app_id, name)``.
    """
    global _backend
    _backend = backend if backend is not None else MemorySecretBackend()


def get(name, default=None, *, app=None, legacy=()):
    """The secret ``name``, else ``BDH_<APP>_<NAME>`` or a ``legacy`` env var, else ``default``."""
    app_id = _app(app)
    check_name(name)
    try:
        value = _backend.get(app_id, name)
    except KeyError:
        value = None
    if value:
        return value
    own = (f"BDH_{app_id.upper()}_{name.upper()}",) if app_id else ()
    for env in (*own, *legacy):
        value = os.environ.get(env)
        if value:
            return value
    return default


def set(name, value, *, app=None):  # noqa: A001 - mirrors get/delete
    """Store ``value`` as the secret ``name`` (replacing any old value)."""
    _backend.set(_app(app), check_name(name), check_value(value))


def delete(name, *, app=None):
    """Forget the secret ``name``; no error if it isn't set."""
    _backend.delete(_app(app), check_name(name))
