"""Spoken announcements on the house speakers, through Home Assistant.

:func:`list_speakers` returns the Sonos and Alexa speakers HA knows about, as
``(entity_id, label)`` pairs ready for a setting's ``choices_from``.
:func:`announce` plays a chime and then speaks a message on the ones picked.

Sonos speakers play the chime from :data:`~bdh_sdk.alerts.CHIMES` (uploaded to
HA's local media on first use, which needs an admin token) and then the
message, both as announcements: Sonos ducks whatever is playing and resumes it
afterwards. Alexa devices (the ``alexa_media`` integration) announce with their
own tone, so they get no chime and keep their own volume.

Announcements are serialized so two at once don't talk over each other.

:func:`tts_audio` returns the spoken message as audio instead, for the hub to
play on its own screens (see ``Alert.speak`` in :mod:`bdh_sdk.alerts`).
Needs ``bdh-sdk[ha]``.
"""
import logging
import threading
import time
import wave
from collections import OrderedDict
from importlib import resources
from urllib.parse import quote

from . import ha
from .alerts import CHIMES, SILENT

__all__ = ["DEFAULT_TTS", "AnnounceError", "announce", "chime_seconds", "list_speakers", "set_backend", "tts_audio"]

log = logging.getLogger(__name__)

DEFAULT_TTS = "tts.home_assistant_cloud"
CHIME_FOLDER = "bdh-chimes"

SONOS = "sonos"
ALEXA = "alexa_media"
_PLATFORMS = {SONOS: "Sonos", ALEXA: "Alexa"}

_lock = threading.Lock()
_uploaded = {}

MAX_TTS_TEXT = 300
_TTS_CACHE_SIZE = 32
_tts_cache = OrderedDict()
_tts_lock = threading.Lock()


class AnnounceError(Exception):
    """Raised when an announcement can't be played on any speaker."""


_backend = None


def set_backend(backend):
    """Route ``list_speakers``, ``announce`` and ``tts_audio`` through
    ``backend`` (the hub broker, for apps in their own container).
    ``None`` restores direct Home Assistant calls."""
    global _backend
    _backend = backend


def _registry():
    """entity_id -> platform for the speakers we can announce on."""
    (entries,) = ha.ws_commands({"type": "config/entity_registry/list"})
    return {
        e["entity_id"]: e["platform"]
        for e in entries
        if e.get("platform") in _PLATFORMS
        and e.get("entity_id", "").startswith("media_player.")
        and not e.get("disabled_by")
    }


def list_speakers():
    """Sonos and Alexa speakers as ``(entity_id, "Name (Sonos)")`` pairs, by name."""
    if _backend is not None:
        return _backend.list_speakers()
    platforms = _registry()
    states = {s["entity_id"]: s for s in ha.get_all_states() if s["entity_id"] in platforms}
    out = []
    for eid, platform in platforms.items():
        state = states.get(eid)
        if state is None:
            continue
        name = state.get("attributes", {}).get("friendly_name") or eid.split(".", 1)[1]
        label = f"{name} ({_PLATFORMS[platform]})"
        if state.get("state") == "unavailable":
            label += " - offline"
        out.append((eid, label))
    return sorted(out, key=lambda p: p[1].lower())


def _chime_file(chime):
    return resources.files("bdh_sdk").joinpath("chimes", f"{chime}.wav")


def chime_seconds(chime):
    """Length of a built-in chime in seconds."""
    with _chime_file(chime).open("rb") as fh, wave.open(fh) as w:
        return w.getnframes() / w.getframerate()


def _chime_media_id(chime):
    if chime not in _uploaded:
        data = _chime_file(chime).read_bytes()
        _uploaded[chime] = ha.upload_media(CHIME_FOLDER, f"{chime}.wav", data, "audio/wav")
    return _uploaded[chime]


def _tts_media_id(message, engine, voice):
    uri = f"media-source://tts/{engine}?message={quote(message)}"
    if voice:
        uri += f"&voice={quote(voice)}"
    return uri


def _play(entity_ids, media_id, volume):
    extra = {"media_content_id": media_id, "media_content_type": "music", "announce": True}
    if volume is not None:
        extra["extra"] = {"volume": int(volume)}
    return ha.call_service("media_player", "play_media", entity_ids, extra)


def announce(message, speakers, volume=None, chime=None, tts_engine=DEFAULT_TTS, voice=None):
    """Speak ``message`` on ``speakers`` (entity ids from :func:`list_speakers`).

    ``volume`` (0-100) applies to Sonos only; ``None`` keeps each speaker's
    own. ``chime`` is a name from :data:`~bdh_sdk.alerts.CHIMES`, or ``None``
    or :data:`~bdh_sdk.alerts.SILENT` for none. ``voice`` is passed to the TTS
    engine (e.g. ``"AvaNeural"`` for Home Assistant Cloud).

    Blocks until the message has been sent. Returns the entity ids that
    failed; raises AnnounceError if every speaker failed.
    """
    if _backend is not None:
        return _backend.announce(message, speakers, volume, chime, tts_engine, voice)
    if not message or not message.strip():
        raise ValueError("message is required")
    if chime not in (None, SILENT) and chime not in CHIMES:
        raise ValueError(f"unknown chime {chime!r}")
    if volume is not None and not 0 <= int(volume) <= 100:
        raise ValueError("volume must be 0-100")
    speakers = list(dict.fromkeys(speakers))
    if not speakers:
        raise AnnounceError("no speakers picked")

    with _lock:
        try:
            platforms = _registry()
        except ha.HAError as exc:
            raise AnnounceError(f"Home Assistant unreachable: {exc}") from exc
        sonos = [s for s in speakers if platforms.get(s) == SONOS]
        alexa = [s for s in speakers if platforms.get(s) == ALEXA]
        failed = [s for s in speakers if s not in sonos and s not in alexa]

        if alexa:
            ok, err = ha.call_service("notify", "alexa_media", None, {
                "message": message, "target": alexa, "data": {"type": "announce"},
            })
            if not ok:
                log.warning("Alexa announce failed: %s", err)
                failed += alexa

        if sonos:
            if chime not in (None, SILENT):
                try:
                    ok, err = _play(sonos, _chime_media_id(chime), volume)
                    if ok:
                        time.sleep(chime_seconds(chime) + 0.3)
                    else:
                        log.warning("chime failed: %s", err)
                except ha.HAError as exc:
                    log.warning("chime upload failed: %s", exc)
            ok, err = _play(sonos, _tts_media_id(message, tts_engine, voice), volume)
            if not ok:
                log.warning("Sonos announce failed: %s", err)
                failed += sonos

    if failed and len(failed) == len(speakers):
        raise AnnounceError("announcement failed on every speaker")
    return failed


def tts_audio(message, tts_engine=DEFAULT_TTS, voice=None):
    """``message`` spoken by Home Assistant, as ``(audio bytes, content type)``.

    Recent messages are cached, so a repeated alert doesn't call HA again.
    Raises ValueError for empty or over-long text and ``ha.HAError`` when HA
    can't make it.
    """
    if _backend is not None:
        return _backend.tts_audio(message, tts_engine, voice)
    message = (message or "").strip()
    if not message:
        raise ValueError("message is required")
    if len(message) > MAX_TTS_TEXT:
        raise ValueError(f"message must be at most {MAX_TTS_TEXT} characters")
    key = (message, tts_engine, voice or "")
    with _tts_lock:
        if key in _tts_cache:
            _tts_cache.move_to_end(key)
            return _tts_cache[key]
    result = ha.get_tts(tts_engine, message, {"voice": voice} if voice else None)
    with _tts_lock:
        _tts_cache[key] = result
        while len(_tts_cache) > _TTS_CACHE_SIZE:
            _tts_cache.popitem(last=False)
    return result
