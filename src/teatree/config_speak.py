"""Resolution of the ``[teatree.speak]`` config sub-table (#2060).

Split out of :mod:`teatree.config` (a god-module): the speak schema —
the ``local`` enum + the ``slack`` bool — is a cohesive concern with a
single dependency (:mod:`teatree.types`). The hook-side mirror lives in
``hook_router._speak_settings``; a parity test pins the two in agreement.
"""

from typing import Any, cast

from teatree.types import LocalPlayback, SpeakConfig

_DEFAULT_SPEAK = SpeakConfig()


def speak_from_subtable(subtable: dict[str, Any], *, base: SpeakConfig = _DEFAULT_SPEAK) -> SpeakConfig:
    """Build a :class:`SpeakConfig` from a ``[teatree.speak]`` sub-table; keys absent fall back to ``base``."""
    local = subtable.get("local")
    presence_backend = subtable.get("presence_backend")
    presence_token_ref = subtable.get("presence_token_ref")
    return SpeakConfig(
        local=LocalPlayback.parse(local) if local is not None else base.local,
        slack=bool(subtable.get("slack", base.slack)),
        presence_backend=str(presence_backend) if presence_backend is not None else base.presence_backend,
        presence_token_ref=str(presence_token_ref) if presence_token_ref is not None else base.presence_token_ref,
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


def parse_speak_setting(raw: object) -> dict[str, bool | str]:
    """Validate + normalise a stored/JSON ``speak`` value to its canonical dict (#1775 DB-home).

    The DB-home registry parser (``OVERLAY_OVERRIDABLE_SETTINGS``): ``config_setting
    set speak`` validates the value through here and stores the canonical
    :meth:`SpeakConfig.to_dict` form (a JSON object), so a bad ``local`` enum is
    rejected at WRITE time and the stored value round-trips back through
    :func:`speak_from_subtable` on read.
    """
    if not isinstance(raw, dict):
        msg = f"Invalid speak value {raw!r}; expected a JSON/TOML table"
        raise TypeError(msg)
    return speak_from_subtable(cast("dict[str, Any]", raw)).to_dict()
