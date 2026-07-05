"""Meeting-aware presence — the LOCAL-TTS auto-mute abstraction (#2171).

Distinct from :class:`teatree.core.availability.PresenceHeartbeat`, which tracks
KEYBOARD activity (a prompt seen recently → the user is at the machine → ``away``
is downgraded to ``present``). This module answers a different question: is the
user CURRENTLY IN A MEETING, so local text-to-speech should stay silent rather
than talk over a call.

A backend (``msteams`` today) answers :meth:`PresenceBackend.current_presence`.
Core never imports the concrete backend: the backends app registers a factory
here at app-ready via :func:`register_presence_backend` — the same
core←backends inversion :mod:`teatree.core.backend_registry` uses — so this
module stays overlay/transport-agnostic and tach-clean.

The playback gate (:func:`teatree.core.speak._speak_local`) treats
:attr:`Presence.IN_MEETING` exactly like ``away``: it suppresses LOCAL ``say``
and nothing else — the Slack audio arm is untouched and the configured
``speak.local`` value is never rewritten. Every non-``IN_MEETING`` verdict
(:attr:`Presence.FREE`, :attr:`Presence.UNKNOWN`) means "do not suppress", so an
unconfigured opt-in, an absent token, or any probe failure fails safe to
audible. The probe result is cached for :data:`_PROBE_TTL_SECONDS` so a per-DM
or per-turn playback never hammers the presence API; a transient ``UNKNOWN`` is
NOT cached so a blip cannot mute (or unmute) for the whole window.
"""

import enum
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, cast

from teatree.types import SpeakConfig

logger = logging.getLogger(__name__)

_PROBE_TTL_SECONDS = 60.0


class Presence(enum.StrEnum):
    """The user's meeting state as a presence backend reports it.

    *   :attr:`FREE` — not in a meeting; local TTS may play.
    *   :attr:`IN_MEETING` — busy / in a call / presenting; local TTS is muted.
    *   :attr:`UNKNOWN` — unconfigured, unreachable, or unclassifiable; treated
        as "do not suppress" everywhere (fail-safe to audible).
    """

    FREE = "free"
    IN_MEETING = "in_meeting"
    UNKNOWN = "unknown"


class PresenceBackend(Protocol):
    def current_presence(self) -> Presence: ...  # pragma: no branch


# Backend factories keyed by their ``[teatree.speak] presence_backend`` name.
# Each factory takes the resolved token ``pass`` ref (read from config by
# :func:`current_presence`, since backends may not import ``teatree.config``)
# and returns a built backend, or ``None`` when it cannot (no token). Populated
# by the backends app at ready-time; empty in a bare ``django.setup()``.
_FACTORIES: dict[str, Callable[[str], PresenceBackend | None]] = {}

_cache_lock = threading.Lock()
_cache: tuple[float, Presence] | None = None


def register_presence_backend(name: str, factory: Callable[[str], PresenceBackend | None]) -> None:
    """Register a presence backend factory under ``name`` (backends → core inversion)."""
    _FACTORIES[name] = factory


def reset_presence_cache() -> None:
    """Drop the cached probe (used by tests and by an explicit config change)."""
    global _cache  # noqa: PLW0603 — single process-wide probe cache
    with _cache_lock:
        _cache = None


def _now() -> float:
    return time.monotonic()


def _effective_speak() -> SpeakConfig:
    """The GLOBAL speak config, read Django-free via ``cold_reader``.

    :func:`current_presence` runs on the local-TTS daemon threads (each bot→user
    DM's local-play leg). A Django ORM read there opens a per-thread connection
    that warns at cross-thread GC — the exact hazard the away gate avoids by
    reading cold. So the meeting gate reads the same stored ``speak`` row via
    :mod:`teatree.config.cold_reader` (one-shot sqlite, closed in-function),
    global scope — a machine-level "am I in a meeting" needs no per-overlay tier.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — Django-free read, safe on the speak daemon threads
    from teatree.config_speak import speak_from_subtable  # noqa: PLC0415 — builds SpeakConfig from the cold dict

    raw = cold_reader.read_setting("speak")
    return speak_from_subtable(cast("dict[str, Any]", raw)) if isinstance(raw, dict) else SpeakConfig()


def _cached_result() -> Presence | None:
    with _cache_lock:
        if _cache is None:
            return None
        recorded_at, value = _cache
    if _now() - recorded_at >= _PROBE_TTL_SECONDS:
        return None
    return value


def _store_result(value: Presence) -> None:
    global _cache  # noqa: PLW0603 — single process-wide probe cache
    with _cache_lock:
        _cache = (_now(), value)


def current_presence() -> Presence:
    """Resolve the user's meeting presence, cached ~60 s — never raises.

    Returns :attr:`Presence.UNKNOWN` (do NOT suppress) when the feature is
    unconfigured (empty ``presence_backend``), the named backend is not
    registered, its factory yields no backend (no token), or the probe fails.
    A known verdict (``FREE`` / ``IN_MEETING``) is cached for
    :data:`_PROBE_TTL_SECONDS`; an ``UNKNOWN`` is not cached so a transient
    failure cannot stick.
    """
    try:
        speak = _effective_speak()
    except Exception as exc:  # noqa: BLE001 — a config read must never mute (or crash) playback
        logger.debug("presence config read failed; treating as unknown: %s", exc)
        return Presence.UNKNOWN
    if not speak.presence_backend:
        return Presence.UNKNOWN
    cached = _cached_result()
    if cached is not None:
        return cached
    result = _probe(speak.presence_backend, speak.presence_token_ref)
    if result is not Presence.UNKNOWN:
        _store_result(result)
    return result


def _probe(name: str, token_ref: str) -> Presence:
    factory = _FACTORIES.get(name)
    if factory is None:
        return Presence.UNKNOWN
    try:
        backend = factory(token_ref)
    except Exception as exc:  # noqa: BLE001 — a backend build failure must never mute playback
        logger.debug("presence backend %r build failed; treating as unknown: %s", name, exc)
        return Presence.UNKNOWN
    if backend is None:
        return Presence.UNKNOWN
    try:
        return backend.current_presence()
    except Exception as exc:  # noqa: BLE001 — a probe failure must never mute playback
        logger.debug("presence backend %r probe failed; treating as unknown: %s", name, exc)
        return Presence.UNKNOWN
