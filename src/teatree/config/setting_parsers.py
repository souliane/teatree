"""Field-value coercers for the config override registries.

The strict TOML/JSON coercers (``_parse_strict_*``), the list coercers, the
``T3_*`` env-tier coercers (``_parse_env_*``), and the handover-path default
helper — the value-parsing layer split out of ``teatree.config.settings`` for
the module-health LOC cap. ``settings`` imports these to build
``OVERLAY_OVERRIDABLE_SETTINGS`` / ``ENV_SETTING_OVERRIDES`` and the
``UserSettings`` defaults, so every ``from teatree.config.settings import
_parse_*`` and ``from teatree.config import _parse_*`` path stays valid.

The strict scalar/list coercion rules themselves live in the Django-free
:mod:`teatree.config.value_coercion` module, shared with the pre-Django
``cold_reader`` (config §3d #5); the ``_parse_*`` wrappers here are the
registry-facing entry points (write-time validation + DB-tier read coercion),
binding the hot-path ``accept_numeric_str=True`` policy.
"""

import os
from collections.abc import Callable
from pathlib import Path

from teatree.config import value_coercion
from teatree.config.enums import TeamsDisplay


def _parse_str_list(raw: object) -> list[str]:
    """Coerce a list-typed overridable setting to ``list[str]``, strictly.

    The single coercer for every list-typed overridable setting, so the write
    path (validation) and the read path (DB-tier coercion) reject a non-list
    scalar identically (#258). See :func:`value_coercion.strict_str_list`.
    """
    return value_coercion.strict_str_list(raw)


_DEFAULT_DISK_CACHE_ALLOWLIST = ("~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes")


def _parse_disk_cache_allowlist(raw: object) -> list[str]:
    """Coerce the disk cache allow-list, falling back to the regenerable-cache default.

    A missing key (``None``) yields the curated default set of regenerable
    caches; an explicit list (even empty) is honoured verbatim so a user can
    narrow the allow-list to nothing. Non-list scalars degrade to the default
    rather than raising. This is the FILE-tier parser (used only by
    ``load_config``); the override tier (per-overlay / DB) uses the strict
    ``_parse_str_list`` which raises on a non-list scalar.
    """
    if not isinstance(raw, list):
        return list(_DEFAULT_DISK_CACHE_ALLOWLIST)
    return [str(s) for s in raw]


_DEFAULT_ON_BEHALF_AUTO_ACTIONS = ("post_e2e_evidence",)


def _parse_env_bool(raw: str) -> bool:
    """Coerce a ``T3_*`` env string to a bool for ``ENV_SETTING_OVERRIDES``.

    Truthy set ``1``/``true``/``yes``/``on`` (case-insensitive); else ``False``.
    """
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# A default-ON ``T3_*`` env flag: present-and-off-value disables, anything else
# enables. Mirrors the legacy ``T3_HOOK_FETCH_TITLES`` semantics so a typo never
# silently disables the feature (the resolver only invokes this when the var is set).
def _parse_env_bool_default_on(raw: str) -> bool:
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _parse_env_positive_int(default: int) -> Callable[[str], int]:
    """A ``T3_*`` env coercer that fails SAFE to *default* on a bad value.

    Returns a parser that accepts a positive integer string and degrades to
    *default* for anything non-positive or non-integer. A pane-budget env var
    (``T3_TEAMS_MAX_PANES`` / ``T3_TEAMS_IDLE_MINUTES``) must never silently
    disable the safety bound by parsing to ``0`` or raising into the resolver —
    the conservative bound cannot be configured away by a typo.
    """

    def parse(raw: str) -> int:
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return parse


def _parse_env_str_list(raw: str) -> list[str]:
    """Coerce a ``T3_*`` comma-separated env string to ``list[str]`` for the env tier.

    Splits on commas and trims each token; an empty string (or a string of only
    separators/whitespace) yields ``[]`` — so ``T3_ON_BEHALF_AUTO_ACTIONS=""``
    clears the allowlist rather than reading as one empty action.
    """
    return [token for token in (part.strip() for part in raw.split(",")) if token]


def _parse_env_teams_display(raw: str) -> TeamsDisplay:
    """Coerce a ``T3_TEAMS_DISPLAY`` env string, failing SAFE to ``NONE`` (#1838 WI-5).

    The presentation-only display mode must never crash the config resolver or
    escalate itself ON via a typo in the env tier: a mistyped value degrades to
    the conservative :attr:`TeamsDisplay.NONE` (no display, in-process path
    unchanged). This is the env-tier counterpart to :meth:`TeamsDisplay.parse`,
    which raises LOUD for the TOML/DB tiers where a write-time validator catches
    the typo at set time.
    """
    try:
        return TeamsDisplay.parse(raw)
    except ValueError:
        return TeamsDisplay.NONE


def _parse_strict_bool(raw: object) -> bool:
    """Coerce a TOML/JSON value for a bool-typed overridable setting, strictly.

    The single coercer for every bool-typed overridable setting: a quoted
    ``"false"`` / a number / a list raises rather than truthy-coercing via
    ``bool(...)`` (``bool("false") == True``, #258). See
    :func:`value_coercion.strict_bool`.
    """
    return value_coercion.strict_bool(raw)


def _parse_strict_int(raw: object) -> int:
    """Coerce a TOML/JSON value for an int-typed overridable setting, strictly.

    Accepts a real ``int`` and a numeric ``str`` (the hot read tier may store
    ``"5"`` — ``accept_numeric_str=True``); REJECTS a ``bool`` (``int(True) ==
    1``, #258) and a ``float`` rather than truncating. See
    :func:`value_coercion.strict_int`.
    """
    return value_coercion.strict_int(raw, accept_numeric_str=True)


def _parse_overridable_positive_int(default: int) -> Callable[[object], int]:
    """An overridable-int coercer that fails SAFE to *default* (mirrors ``_parse_env_positive_int``).

    Used for the pane-budget settings (``teams_max_panes`` / ``teams_idle_minutes``)
    in ``OVERLAY_OVERRIDABLE_SETTINGS``: a per-overlay or DB-tier value that is
    non-positive, a ``bool``, a ``float``, or a non-numeric string degrades to
    *default* rather than raising into the config resolver. The safety bound the
    setting encodes cannot be disabled by a mistyped override.
    """

    def parse(raw: object) -> int:
        if isinstance(raw, bool):
            return default
        if isinstance(raw, int):
            return raw if raw > 0 else default
        if isinstance(raw, str):
            try:
                value = int(raw.strip())
            except ValueError:
                return default
            return value if value > 0 else default
        return default

    return parse


def _parse_strict_float(raw: object) -> float:
    """Coerce a TOML/JSON value for a float-typed overridable setting, strictly.

    Accepts a real ``float``, an ``int`` (a TOML ``25`` for a float setting is
    legitimate), and a numeric ``str``; REJECTS a ``bool`` (``float(True) ==
    1.0``, #258). See :func:`value_coercion.strict_float`.
    """
    return value_coercion.strict_float(raw)


def _parse_strict_str(raw: object) -> str:
    """Coerce a TOML/JSON value for a str-typed overridable setting, strictly.

    Accepts only a real ``str``; REJECTS a ``bool``/``int``/``float``/``list``
    rather than stringifying it via ``str(...)`` (``str(True) == "True"``, #258).
    See :func:`value_coercion.strict_str`.
    """
    return value_coercion.strict_str(raw)


def _parse_handover_mirror_path(raw: object) -> Path:
    # Path-typed field (consumed as ``.parent`` / ``.is_file()``), so it must resolve
    # to a real ``Path`` — unlike the str-accessor fields. An empty value means "unset"
    # → the default, matching the pre-DB TOML semantics (absent/empty fell back).
    return Path(stored).expanduser() if (stored := _parse_strict_str(raw)) else _default_handover_mirror_path()


def _parse_user_identity_aliases(raw: object) -> list[str]:
    """Coerce a TOML list of usernames/handles to ``list[str]``.

    Returns a deduped list of non-empty alias handles, in insertion order.
    A non-list SCALAR raises ``TypeError`` (#258) — a scalar for a list-typed
    setting is a type error that must be loud, never silently degraded to an
    empty list (which would mask a corrupt override). Consumed by the ticket-disposition
    scanner (#975) to suppress reassign signals between the operator's own
    identities, and by the loop's PR/MR scanners (#976) to union-query each
    alias so cross-forge work surfaces in the statusline.
    """
    if not isinstance(raw, list):
        msg = f"Invalid user_identity_aliases value {raw!r}; expected a JSON/TOML array, not a scalar"
        raise TypeError(msg)
    return list(dict.fromkeys(str(s) for s in raw if isinstance(s, str) and s))


def _default_handover_mirror_path() -> Path:
    """Human-readable mirror of the latest session hand-off.

    ``${XDG_STATE_HOME:-~/.local/state}/teatree/handover/latest.md`` — XDG
    *state* (not data) because a hand-off is regenerable transient session
    state, not durable user data. Overridable via ``[teatree]
    handover_mirror_path``. The DB row is the source of truth; this file
    is for human-readability and for bootstrapping a brand-new session.
    """
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "teatree" / "handover" / "latest.md"
