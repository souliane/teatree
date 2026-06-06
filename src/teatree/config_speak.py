"""Resolution of the ``[teatree.speak]`` config sub-table (#2050).

Split out of :mod:`teatree.config` (a god-module): the speak schema —
the new ``local`` / ``slack_audio`` / ``scope`` sub-table plus the
one-transition-release legacy auto-map for ``speak_mode`` / ``speak_target``
— is a cohesive concern with a single dependency (:mod:`teatree.types`).
The hook-side mirror lives in ``hook_router._speak_settings``; a parity
test pins the two in agreement.
"""

from typing import Any

from teatree.types import SpeakConfig, SpeakScope

_DEFAULT_SPEAK = SpeakConfig()


def speak_from_subtable(subtable: dict[str, Any], *, base: SpeakConfig = _DEFAULT_SPEAK) -> SpeakConfig:
    """Build a :class:`SpeakConfig` from a ``[teatree.speak]`` sub-table; keys absent fall back to ``base``."""
    scope = subtable.get("scope")
    return SpeakConfig(
        local=bool(subtable.get("local", base.local)),
        slack_audio=bool(subtable.get("slack_audio", base.slack_audio)),
        scope=SpeakScope.parse(scope) if scope is not None else base.scope,
    )


def speak_from_legacy(teatree: dict[str, Any]) -> SpeakConfig:
    """Map legacy ``speak_mode`` / ``speak_target`` to a :class:`SpeakConfig` (one-release auto-map).

    ``speak_mode`` drives the scope (``all`` → ``all``, else ``dm``); ``off``
    forces both destinations off. ``speak_target`` drives the booleans.
    """
    mode = str(teatree.get("speak_mode", "im-only")).strip().lower()
    target = str(teatree.get("speak_target", "local")).strip().lower()
    if mode == "off":
        return SpeakConfig(scope=SpeakScope.DM)
    return SpeakConfig(
        local=target in {"local", "both"},
        slack_audio=target in {"slack-audio", "both"},
        scope=SpeakScope.ALL if mode == "all" else SpeakScope.DM,
    )


def resolve_speak(teatree: dict[str, Any]) -> SpeakConfig:
    """Resolve the effective :class:`SpeakConfig`: new sub-table wins, else legacy map, else defaults.

    The CONFIGURED value only; the binary-presence gate lives in
    :func:`teatree.core.speak.resolve_speak`.
    """
    subtable = teatree.get("speak")
    if isinstance(subtable, dict):
        return speak_from_subtable(subtable)
    if "speak_mode" in teatree or "speak_target" in teatree:
        return speak_from_legacy(teatree)
    return SpeakConfig()
