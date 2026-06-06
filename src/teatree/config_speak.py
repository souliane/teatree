"""Resolution of the ``[teatree.speak]`` config sub-table (#2060).

Split out of :mod:`teatree.config` (a god-module): the speak schema —
the ``local`` enum + the ``slack`` bool — is a cohesive concern with a
single dependency (:mod:`teatree.types`). The hook-side mirror lives in
``hook_router._speak_settings``; a parity test pins the two in agreement.
"""

from typing import Any

from teatree.types import LocalPlayback, SpeakConfig

_DEFAULT_SPEAK = SpeakConfig()


def speak_from_subtable(subtable: dict[str, Any], *, base: SpeakConfig = _DEFAULT_SPEAK) -> SpeakConfig:
    """Build a :class:`SpeakConfig` from a ``[teatree.speak]`` sub-table; keys absent fall back to ``base``."""
    local = subtable.get("local")
    return SpeakConfig(
        local=LocalPlayback.parse(local) if local is not None else base.local,
        slack=bool(subtable.get("slack", base.slack)),
    )


def resolve_speak(teatree: dict[str, Any]) -> SpeakConfig:
    """Resolve the effective :class:`SpeakConfig`: the ``[teatree.speak]`` sub-table, else defaults.

    The CONFIGURED value only; the binary-presence gate lives in
    :func:`teatree.core.speak.resolve_speak`.
    """
    subtable = teatree.get("speak")
    if isinstance(subtable, dict):
        return speak_from_subtable(subtable)
    return SpeakConfig()
