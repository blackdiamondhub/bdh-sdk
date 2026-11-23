"""Which screens are checking in with the hub.

Every hub page posts a heartbeat about once a minute. :func:`status` returns
the last one per client IP, so an app (a screen watchdog, say) can tell a
screen that went quiet from one that is fine. Needs the ``displays.read``
permission.

``ready_since`` is when the hub started serving pages. A screen can't have
checked in before then, so treat a quiet screen as unknown until the hub has
been up for longer than your staleness limit.
"""
from datetime import datetime

__all__ = ["status"]


def status():
    """``{"ready_since", "now", "clients": [{"ip", "last_seen", "age_seconds"}]}``.

    ``ready_since``, ``now`` and ``last_seen`` are timezone-aware datetimes;
    ``age_seconds`` is an int measured on the hub's clock.
    """
    from . import rpc

    raw = rpc.client().call("displays.status") or {}
    return {
        "ready_since": _when(raw.get("ready_since")),
        "now": _when(raw.get("now")),
        "clients": [
            {"ip": c.get("ip"), "last_seen": _when(c.get("last_seen")),
             "age_seconds": c.get("age_seconds")}
            for c in raw.get("clients") or () if isinstance(c, dict)
        ],
    }


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None
