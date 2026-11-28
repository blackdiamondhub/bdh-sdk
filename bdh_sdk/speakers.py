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

The spoken text comes from one of HA's TTS engines. By default
(``tts_engine=None``) the first Google engine HA has is used (free, no
account), then Home Assistant Cloud, then any other. A requested engine that
HA doesn't have (e.g. Nabu Casa signed out) falls back to that default
instead of failing. ``voice`` only applies to Home Assistant Cloud and is
dropped for other engines.

The hub can make the speech itself instead: :func:`set_synth` installs a
function ``synth(message, voice) -> (audio bytes, content type)``. Sonos then
plays that audio, uploaded once to HA's local media (``bdh-tts/<sha256>.mp3``)
and still played as an announcement, and :func:`tts_audio` returns it. When the
synth raises, the HA engine path above is used instead.

:func:`tts_audio` returns the spoken message as audio instead, for the hub to
play on its own screens (see ``Alert.speak`` in :mod:`bdh_sdk.alerts`).
Needs ``bdh-sdk[ha]``.

On a hub (SDK 0.50+) every call goes through the hub's own speaker layer
(:func:`set_backend`), which plays on Sonos directly and keeps accepting the
HA entity ids above. It adds :func:`devices` (each speaker's native id, kind
and capabilities), :func:`play` (an app's own short clip) and
``announce(..., detail=True)`` (a result per speaker). :func:`ha_list_speakers`,
:func:`ha_announce` and :func:`ha_tts_audio` are the Home Assistant path on
its own, whatever the backend.
"""
import hashlib
import logging
import threading
import time
import wave
from collections import OrderedDict
from importlib import resources
from urllib.parse import quote

from . import ha
from .alerts import CHIMES, MAX_TTS_TEXT, SILENT

__all__ = [
    "DEFAULT_TTS", "FAILED", "MAX_PLAY_BYTES", "MAX_PLAY_SECONDS", "OFFLINE", "PLAYED", "UNSUPPORTED",
    "AnnounceError", "announce", "canonical", "chime_seconds", "devices", "ha_announce", "ha_list_speakers",
    "ha_tts_audio", "list_speakers", "play", "set_backend", "set_synth", "tts_audio",
]

log = logging.getLogger(__name__)

DEFAULT_TTS = None
HA_CLOUD_TTS = "tts.home_assistant_cloud"
_ENGINES_TTL = 300
_engines = {"at": 0.0, "ids": []}
CHIME_FOLDER = "bdh-chimes"
CLIP_FOLDER = "bdh-tts"
_CLIPS_MAX = 512
_EXTENSIONS = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/ogg": "ogg", "audio/flac": "flac"}

SONOS = "sonos"
ALEXA = "alexa_media"
_PLATFORMS = {SONOS: "Sonos", ALEXA: "Alexa"}

_lock = threading.Lock()
_uploaded = {}
_clips = OrderedDict()  # (HA url, sha256 of the audio) -> media id

_TTS_CACHE_SIZE = 32

# A speaker's result in announce(..., detail=True) and play(..., detail=True).
PLAYED, FAILED, UNSUPPORTED, OFFLINE = "played", "failed", "unsupported", "offline"

# speakers.play limits (the hub checks them too).
MAX_PLAY_BYTES = 2 * 1024 * 1024
MAX_PLAY_SECONDS = 60
_tts_cache = OrderedDict()
_tts_lock = threading.Lock()


class AnnounceError(Exception):
    """Raised when an announcement can't be played on any speaker."""


_backend = None


def set_backend(backend):
    """Route ``list_speakers``, ``announce``, ``tts_audio``, ``devices`` and
    ``play`` through ``backend`` (the hub broker for apps in their own
    container, the hub's speaker layer in the hub). ``None`` restores direct
    Home Assistant calls.

    A backend has ``list_speakers()``, ``announce(message, speakers, volume,
    chime, tts_engine, voice)`` and ``tts_audio(message, tts_engine, voice)``;
    ``devices()``, ``play(audio, speakers, volume, night, detail)`` and
    ``announce(..., detail=True)`` are optional (SDK 0.50+): ``detail`` is
    only passed when a caller asks for it."""
    global _backend
    _backend = backend


_synth = None


def set_synth(synth):
    """Make the speech with ``synth(message, voice)`` instead of HA's TTS.

    It returns ``(audio bytes, content type)``, ``None`` to let HA's TTS
    engine speak this time, or raises; on an exception :func:`announce` and
    :func:`tts_audio` fall back to HA's TTS engine too (logged).
    ``None`` goes back to HA's TTS only.
    """
    global _synth
    _synth = synth


def _synthesize(message, voice):
    """The synth's audio, or ``None`` (logged) when there is no synth or it failed."""
    if _synth is None:
        return None
    try:
        result = _synth(message, voice)
        if result is None:
            return None
        audio, content_type = result
        if not audio:
            raise ValueError("no audio")
        return audio, content_type
    except Exception as exc:
        log.warning("speech synth failed, using Home Assistant TTS: %s", exc)
        return None


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
    """Speakers as ``(id, "Name (Sonos)")`` pairs, by name.

    Directly from HA these are the Sonos and Alexa entity ids; on a hub the
    HA entity id whenever the speaker has one, else the hub's native id."""
    if _backend is not None:
        return _backend.list_speakers()
    return ha_list_speakers()


def _ha_speakers():
    """``[(entity_id, platform, name, online)]`` for HA's Sonos and Alexa speakers."""
    platforms = _registry()
    states = {s["entity_id"]: s for s in ha.get_all_states() if s["entity_id"] in platforms}
    out = []
    for eid, platform in platforms.items():
        state = states.get(eid)
        if state is None:
            continue
        name = state.get("attributes", {}).get("friendly_name") or eid.split(".", 1)[1]
        out.append((eid, platform, name, state.get("state") != "unavailable"))
    return out


def ha_list_speakers():
    """:func:`list_speakers` straight from Home Assistant, ignoring any backend."""
    out = []
    for eid, platform, name, online in _ha_speakers():
        label = f"{name} ({_PLATFORMS[platform]})"
        if not online:
            label += " - offline"
        out.append((eid, label))
    return sorted(out, key=lambda p: p[1].lower())


def devices():
    """Every speaker with what it can do, by name.

    Dicts with ``id`` (the hub's native id, e.g. ``sonos:RINCON_...``),
    ``legacy_id`` (the HA entity id it is also known by, or ``None``),
    ``name``, ``kind`` (``sonos``, ``alexa``, ``ha`` ...), ``caps``
    (``{"audio", "duck", "text", "volume"}`` booleans: plays audio clips,
    ducks the music, speaks text itself, takes a volume) and ``online``.
    Both ids work everywhere a speaker id is taken.
    """
    if _backend is not None:
        fn = getattr(_backend, "devices", None)
        if fn is not None:
            return fn()
        return [_from_pair(i, label) for i, label in _backend.list_speakers()]
    out = []
    for eid, platform, name, online in _ha_speakers():
        sonos = platform == SONOS
        out.append({"id": eid, "legacy_id": None, "name": name, "kind": "sonos" if sonos else "alexa",
                    "caps": {"audio": sonos, "duck": sonos, "text": not sonos, "volume": sonos},
                    "online": online})
    return sorted(out, key=lambda d: d["name"].lower())


def _from_pair(speaker_id, label):
    """A :func:`devices` entry from a ``list_speakers`` pair (a hub without ``devices``)."""
    online = not label.endswith(" - offline")
    name = label[:-len(" - offline")] if not online else label
    kind = "unknown"
    for platform, title in _PLATFORMS.items():
        if name.endswith(f" ({title})"):
            name, kind = name[:-len(title) - 3], "sonos" if platform == SONOS else "alexa"
    sonos = kind == "sonos"
    return {"id": speaker_id, "legacy_id": None, "name": name, "kind": kind,
            "caps": {"audio": sonos, "duck": sonos, "text": kind == "alexa", "volume": sonos},
            "online": online}


def canonical(ids):
    """``ids`` with each speaker's HA entity id swapped for its native id.

    For apps converting saved picks: ids the hub doesn't know (or already
    native) come back unchanged, in order, without duplicates."""
    by_alias = {}
    for d in devices():
        by_alias[d["id"]] = d["id"]
        if d.get("legacy_id"):
            by_alias.setdefault(d["legacy_id"], d["id"])
    return list(dict.fromkeys(by_alias.get(i, i) for i in ids))


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


def _clip_media_id(audio, content_type):
    """Media id of ``audio`` in HA's local media, uploading it the first time."""
    sha = hashlib.sha256(audio).hexdigest()
    try:
        where = ha._base_url()
    except Exception:
        where = ""
    key = (where, sha)
    if key in _clips:
        _clips.move_to_end(key)
        return _clips[key]
    ext = _EXTENSIONS.get((content_type or "").split(";")[0].strip().lower(), "mp3")
    media_id = ha.upload_media(CLIP_FOLDER, f"{sha}.{ext}", audio, content_type or "audio/mpeg")
    _clips[key] = media_id
    while len(_clips) > _CLIPS_MAX:
        _clips.popitem(last=False)
    return media_id


def _tts_engines():
    """HA's TTS entity ids, refreshed every few minutes."""
    now = time.monotonic()
    if not _engines["ids"] or now - _engines["at"] > _ENGINES_TTL:
        ids = sorted(s["entity_id"] for s in ha.get_all_states() if s.get("entity_id", "").startswith("tts."))
        _engines.update(at=now, ids=ids)
    return _engines["ids"]


def _engine(requested):
    """The engine to use: ``requested`` if HA has it, else Google, HA Cloud, any."""
    try:
        ids = _tts_engines()
    except ha.HAError:
        return requested or HA_CLOUD_TTS
    if requested in ids:
        return requested
    for pick in [i for i in ids if i.startswith("tts.google")] + [HA_CLOUD_TTS] + ids:
        if pick in ids:
            if requested:
                log.warning("TTS engine %s not in HA, using %s", requested, pick)
            return pick
    return requested or HA_CLOUD_TTS


def _voice_options(engine, voice):
    return {"voice": voice} if voice and engine == HA_CLOUD_TTS else None


def _tts_media_id(message, engine, voice):
    uri = f"media-source://tts/{engine}?message={quote(message)}"
    if _voice_options(engine, voice):
        uri += f"&voice={quote(voice)}"
    return uri


def _play(entity_ids, media_id, volume):
    extra = {"media_content_id": media_id, "media_content_type": "music", "announce": True}
    if volume is not None:
        extra["extra"] = {"volume": int(volume)}
    return ha.call_service("media_player", "play_media", entity_ids, extra)


def check_message(message):
    """``message`` stripped; ValueError when it's empty or over :data:`MAX_TTS_TEXT` characters."""
    message = message.strip() if isinstance(message, str) else ""
    if not message:
        raise ValueError("message is required")
    if len(message) > MAX_TTS_TEXT:
        raise ValueError(f"message must be at most {MAX_TTS_TEXT} characters")
    return message


def announce(message, speakers, volume=None, chime=None, tts_engine=DEFAULT_TTS, voice=None, *, detail=False):
    """Speak ``message`` on ``speakers`` (ids from :func:`list_speakers` or :func:`devices`).

    ``volume`` (0-100) applies to Sonos only; ``None`` keeps each speaker's
    own. ``chime`` is a name from :data:`~bdh_sdk.alerts.CHIMES`, or ``None``
    or :data:`~bdh_sdk.alerts.SILENT` for none. ``voice`` is passed to the
    synth (see :func:`set_synth`) and to the TTS engine when it's Home
    Assistant Cloud (e.g. ``"AvaNeural"``).

    Blocks until the message has been sent. Returns the ids that failed;
    raises AnnounceError if every speaker failed. With ``detail=True`` it
    returns ``{id: "played" | "failed" | "unsupported" | "offline"}`` instead
    and doesn't raise when speakers fail (SDK 0.50+; a hub from before
    answers with the failed list, turned into ``played``/``failed``).
    Raises ValueError for an empty message or one over :data:`MAX_TTS_TEXT`
    characters, before anything plays.
    """
    check_message(message)
    if _backend is not None:
        if not detail:
            return _backend.announce(message, speakers, volume, chime, tts_engine, voice)
        result = _backend.announce(message, speakers, volume, chime, tts_engine, voice, detail=True)
        if isinstance(result, list):
            return {s: FAILED if s in result else PLAYED for s in dict.fromkeys(speakers)}
        return result
    return ha_announce(message, speakers, volume, chime, tts_engine, voice, detail=detail)


def play(audio, speakers, volume=None, *, night=False, detail=False):
    """Play ``audio`` (MP3 or WAV bytes, at most 2 MB and 60 s) on ``speakers``.

    Only speakers that play clips do (``caps["audio"]``); text-only ones
    (Alexa) are ``unsupported``. Sonos ducks the music and resumes it. The
    hub caps ``volume`` at its maximum and refuses during the display's
    night hours unless ``night=True``. Needs the hub (SDK 0.50+): directly
    against Home Assistant it raises AnnounceError.

    Returns the ids that failed (raises AnnounceError if all did), or with
    ``detail=True`` ``{id: "played" | "failed" | "unsupported" | "offline"}``.
    """
    if not isinstance(audio, (bytes, bytearray)) or not audio:
        raise ValueError("audio must be bytes")
    if len(audio) > MAX_PLAY_BYTES:
        raise ValueError(f"audio must be at most {MAX_PLAY_BYTES // (1024 * 1024)} MB")
    if volume is not None and not 0 <= int(volume) <= 100:
        raise ValueError("volume must be 0-100")
    speakers = list(dict.fromkeys(speakers))
    if not speakers:
        raise AnnounceError("no speakers picked")
    fn = getattr(_backend, "play", None)
    if fn is None:
        raise AnnounceError("playing clips needs the hub's speakers (bdh-sdk 0.50 on the hub)")
    return fn(bytes(audio), speakers, volume, night=bool(night), detail=bool(detail))


def ha_announce(message, speakers, volume=None, chime=None, tts_engine=DEFAULT_TTS, voice=None, *,
                detail=False):
    """:func:`announce` straight through Home Assistant, ignoring any backend."""
    check_message(message)
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
            # Made before the chime so the message follows it straight away.
            speech = None
            clip = _synthesize(message, voice)
            if clip is not None:
                try:
                    speech = _clip_media_id(*clip)
                except ha.HAError as exc:
                    log.warning("speech upload failed, using Home Assistant TTS: %s", exc)
            if chime not in (None, SILENT):
                try:
                    ok, err = _play(sonos, _chime_media_id(chime), volume)
                    if ok:
                        time.sleep(chime_seconds(chime) + 0.3)
                    else:
                        log.warning("chime failed: %s", err)
                except ha.HAError as exc:
                    log.warning("chime upload failed: %s", exc)
            ok = False
            if speech is not None:
                ok, err = _play(sonos, speech, volume)
                if not ok:
                    log.warning("Sonos announce of the hub's speech failed, using Home Assistant TTS: %s", err)
            if not ok:
                ok, err = _play(sonos, _tts_media_id(message, _engine(tts_engine), voice), volume)
            if not ok:
                log.warning("Sonos announce failed: %s", err)
                failed += sonos

    if detail:
        return {s: FAILED if s in failed else PLAYED for s in speakers}
    if failed and len(failed) == len(speakers):
        raise AnnounceError("announcement failed on every speaker")
    return failed


def tts_audio(message, tts_engine=DEFAULT_TTS, voice=None):
    """``message`` spoken, as ``(audio bytes, content type)``.

    From the synth when one is set (see :func:`set_synth`; it does its own
    caching), else from Home Assistant, with recent messages cached so a
    repeated alert doesn't call HA again.
    Raises ValueError for empty or over-long text and ``ha.HAError`` when HA
    can't make it.
    """
    if _backend is not None:
        return _backend.tts_audio(message, tts_engine, voice)
    return ha_tts_audio(message, tts_engine, voice)


def ha_tts_audio(message, tts_engine=DEFAULT_TTS, voice=None):
    """:func:`tts_audio` from the synth or Home Assistant, ignoring any backend."""
    message = check_message(message or "")
    clip = _synthesize(message, voice)
    if clip is not None:
        return clip
    tts_engine = _engine(tts_engine)
    voice = voice if _voice_options(tts_engine, voice) else None
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
