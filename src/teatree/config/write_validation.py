"""The one parse→coerce→canonicalize core every config WRITE surface shares.

Four surfaces persist a ``ConfigSetting`` row — ``config_setting set`` / ``seed``
(the CLI), the dashboard settings editor POST, and the ``config_migration`` TOML
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

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS

# A canonical, JSON/TOML-storable config value — the shape every registry parser
# returns (a ``StrEnum`` is a ``str``; the structured ``speak`` / ``mr_reminder``
# parsers return plain dicts). Mirrors ``ConfigSetting.ConfigValue``, which lives in
# the ``core.models`` layer this platform module cannot import.
type ConfigWriteValue = bool | int | float | str | list[object] | dict[str, object]


class ConfigWriteError(ValueError):
    """A raw config value that a setting's registry parser rejected (#258)."""


def validate_config_write(key: str, raw: object) -> ConfigWriteValue:
    """Coerce an already-decoded *raw* value through *key*'s registry parser.

    Returns the CANONICAL value to persist (a numeric string ``"5"`` → ``5``, an
    upper-case enum ``"AUTO"`` → ``"auto"``) so the stored row and the read-time
    coercion agree. Raises :class:`ConfigWriteError` — wrapping the parser's
    ``ValueError`` / ``TypeError`` / ``AttributeError`` — so every write surface
    reports an invalid value identically and leaves its store untouched.

    The caller MUST have already gated *key* into
    :data:`~teatree.config.known_settings.ALL_KNOWN_CONFIG_SETTINGS`; this coerces
    a known key's value, it does not decide key-ness or any surface-specific gate.
    """
    parser = ALL_KNOWN_CONFIG_SETTINGS[key]
    try:
        return parser(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigWriteError(str(exc)) from exc
