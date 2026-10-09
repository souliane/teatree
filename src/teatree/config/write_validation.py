"""The one parse→coerce→canonicalize core every config WRITE surface shares.

Five surfaces persist a ``ConfigSetting`` row — ``config_setting set`` / ``seed``
(the CLI), the dashboard settings editor POST, the Django admin, and the ``config_interchange`` TOML
import. Each ran the SAME copy: look up the key's registry parser, coerce the raw
value, and catch the same ``(ValueError, TypeError, AttributeError)`` tuple a bad
value raises. This module owns that core once so the surfaces can never drift on
how a value is coerced or which errors a bad value raises.

Each surface keeps its OWN gating (known-key refusal, the dashboard safety-posture
confirm, the import removed-key + secret scan) and its OWN persistence call and
error presentation (``SystemExit`` vs ``HttpResponseBadRequest`` vs a reject row);
this helper covers only the shared coercion, raising a single
:class:`ConfigWriteError` the caller formats for its surface.
"""

from teatree.config.extra_headers import UNLISTED_HEADER_REFUSAL, carries_unlisted_header
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.settings import WRITE_CONCURRENCY_PER_CORE_MAX, WRITE_CONCURRENCY_PER_CORE_MIN

# A canonical, JSON/TOML-storable config value — the shape every registry parser
# returns (a ``StrEnum`` is a ``str``; the structured ``speak`` / ``mr_reminder``
# parsers return plain dicts). Mirrors ``ConfigSetting.ConfigValue``, which lives in
# the ``core.models`` layer this platform module cannot import.
type ConfigWriteValue = bool | int | float | str | list[object] | dict[str, object]

#: The reader clamps these silently, so a write outside the range is refused where the operator sees it.
_WRITE_RANGES: dict[str, tuple[float, float]] = {
    "admission_write_concurrency_per_core": (WRITE_CONCURRENCY_PER_CORE_MIN, WRITE_CONCURRENCY_PER_CORE_MAX),
}


class ConfigWriteError(ValueError):
    """A raw config value that a setting's registry parser rejected (#258)."""


def validate_config_write(key: str, raw: object) -> ConfigWriteValue:
    """Coerce an already-decoded *raw* value through *key*'s registry parser.

    Returns the CANONICAL value to persist (a numeric string ``"5"`` → ``5``, an
    upper-case enum ``"AUTO"`` → ``"auto"``) so the stored row and the read-time
    coercion agree. Raises :class:`ConfigWriteError` — wrapping the parser's
    ``ValueError`` / ``TypeError`` / ``AttributeError`` — so every write surface
    reports an invalid value identically and leaves its store untouched.

    Unknown and retired keys are refused here as well, so a direct caller never
    receives an unhandled ``KeyError``. Surfaces still own their more specific
    key and permission checks. An extra-headers map naming a header off its
    allowlist is refused here, because its parser only withholds one.
    """
    if carries_unlisted_header(key, raw):
        raise ConfigWriteError(UNLISTED_HEADER_REFUSAL)
    parser = ALL_KNOWN_CONFIG_SETTINGS.get(key)
    if parser is None:
        msg = f"unknown config key: {key}"
        raise ConfigWriteError(msg)
    try:
        value = parser(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigWriteError(str(exc)) from exc
    if (bounds := _WRITE_RANGES.get(key)) and not bounds[0] <= value <= bounds[1]:
        msg = f"{key} must be between {bounds[0]:g} and {bounds[1]:g}, got {raw!r}"
        raise ConfigWriteError(msg)
    return value
