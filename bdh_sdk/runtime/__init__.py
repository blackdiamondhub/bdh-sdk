"""Run one hub app on its own, in its container.

The supervisor starts ``python -m bdh_sdk.runtime`` with:

- ``BDH_APP_ID``: the app to serve (its ``blackdiamondhub.plugins`` entry point).
- ``BDH_BROKER_URL`` / ``BDH_APP_TOKEN``: the hub broker and this app's token.
- ``BDH_PORT`` (default 8000), ``BDH_DATA_DIR`` (default ``/data``).
- ``BDH_HUB_BRAND`` (JSON), ``BDH_TIME_ZONE``, ``BDH_SECRET_KEY`` (optional).

Requires the ``runtime`` extra.
"""
import os
from functools import lru_cache

__all__ = ["app_spec", "main"]

APP_ENV = "BDH_APP_ID"


@lru_cache(maxsize=1)
def app_spec():
    """The :class:`~bdh_sdk.plugins.PluginSpec` named by ``BDH_APP_ID``."""
    from importlib.metadata import entry_points

    from ..plugins import ENTRY_POINT_GROUP, PluginSpec

    app_id = os.environ.get(APP_ENV, "")
    if not app_id:
        raise RuntimeError(f"{APP_ENV} is not set")
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name != app_id:
            continue
        spec = ep.load()
        if not isinstance(spec, PluginSpec) or spec.id != app_id:
            raise RuntimeError(f"entry point {ep.name!r} is not the PluginSpec for {app_id!r}")
        return spec
    raise RuntimeError(f"no {ENTRY_POINT_GROUP} entry point named {app_id!r}")


def setup():
    """Configure Django for the app and point the SDK at the broker."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bdh_sdk.runtime.settings")
    from .. import rpc

    if rpc.configured() and not rpc.installed():
        rpc.install()
    import django

    django.setup()


def main(argv=None):
    """Migrate the app's database, then serve it with uvicorn (one worker)."""
    import uvicorn
    from django.core.management import call_command

    setup()
    call_command("migrate", interactive=False, verbosity=1)
    uvicorn.run(
        "bdh_sdk.runtime.asgi:application",
        host=os.environ.get("BDH_HOST", "0.0.0.0"),
        port=int(os.environ.get("BDH_PORT", "8000")),
        workers=1,
        lifespan="off",
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )
