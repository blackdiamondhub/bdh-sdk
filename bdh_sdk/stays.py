"""Who is staying at the lodge right now.

:func:`current` asks the hub for the stay in progress, the same one the
homepage uses for guest mode, so an app (a picture frame, say) doesn't need
its own source. Needs the ``stays.read`` permission (container apps only).

The hub caches the answer, keeps the last good one when the app that reports
stays fails, and drops a stay once its checkout has passed. Broker failures
raise :class:`bdh_sdk.rpc.BrokerError` (``PermissionDenied`` without the
grant) rather than returning None, so callers can keep their last known state.
"""
import hashlib
from dataclasses import dataclass
from datetime import datetime

from .plugins import Stay

__all__ = ["CurrentStay", "current", "key"]


@dataclass(frozen=True)
class CurrentStay:
    """The stay in progress and its opaque ``key``.

    ``key`` is the id the hub also sends in the visitor identity
    (``request.bdh_stay_key``), so per-stay data can be matched or reset when
    new guests arrive.
    """

    stay: Stay
    key: str


def key(guest, arrival=None):
    """The hub's opaque stay key: 12 hex chars from the guest and arrival."""
    when = arrival.isoformat() if arrival else ""
    return hashlib.sha256(f"{guest}|{when}".encode()).hexdigest()[:12]


def current():
    """The :class:`CurrentStay` in progress, or None between stays.

    Datetimes are timezone-aware.
    """
    from . import rpc

    raw = (rpc.client().call("stays.current") or {}).get("stay")
    if not isinstance(raw, dict) or not raw.get("guest"):
        return None
    stay = Stay(guest=raw["guest"], arrival=_when(raw.get("arrival")),
                checkout=_when(raw.get("checkout")), owner=bool(raw.get("owner")))
    return CurrentStay(stay=stay, key=raw.get("key") or key(stay.guest, stay.arrival))


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None
