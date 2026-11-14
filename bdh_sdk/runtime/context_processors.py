"""Template context the hub's own pages would give an app's templates."""
import base64
from functools import lru_cache

from django.urls import NoReverseMatch, reverse


def _data_uri(svg):
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


@lru_cache(maxsize=1)
def _brand():
    from bdh_sdk.branding import brand, monogram_svg

    info = brand()
    return {
        "name": info["name"],
        "subtitle": info["subtitle"],
        "monogram": _data_uri(monogram_svg()),
        "monogram_light": _data_uri(monogram_svg(light=True)),
    }


def _tile_url(tile):
    try:
        return reverse(tile.url_name)
    except NoReverseMatch:
        return None


def nav_title(path):
    """Title of the tile whose URL is ``path``, else the app's first tile."""
    from . import app_spec

    tiles = app_spec().tiles
    if not tiles:
        return ""
    exact = next((t for t in tiles if _tile_url(t) == path), None)
    return (exact or tiles[0]).title


def hub(request):
    """``hub_brand``, ``hub_nav_title``, ``hub_stay_key``, ``hub_mode``, ``hub_origin`` and ``user``."""
    return {
        "hub_brand": _brand(),
        "hub_nav_title": nav_title(request.path),
        "hub_stay_key": getattr(request, "bdh_stay_key", "") or "",
        "hub_mode": getattr(request, "bdh_mode", "") or "",
        "hub_origin": getattr(request, "bdh_hub_origin", "") or "",
        "user": getattr(request, "user", None),
    }
