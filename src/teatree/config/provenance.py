"""Where a setting's effective value actually CAME FROM — the resolution chain, named.

A settings surface can show a value; only provenance answers the operator's real
question, "why is it that". The dashboard used to answer it with the setting's KIND
(``default`` / ``personal`` / ``secret``), which reads ``default`` for hundreds of
consecutive rows and, sitting beside a *shipped default* column, is misread as "this
value came from the default" — while the row right beside it says the value differs
from that default.

There is exactly ONE resolution here: the tiers are read through
:func:`~teatree.config.resolution.read_setting_layers` and
:func:`~teatree.config.resolution.read_env_setting_overrides`, the same seams
:func:`~teatree.config.resolution.get_effective_settings` folds into a ``UserSettings``.
This module walks those tiers instead of folding them, so the value and the layer that
supplied it can never disagree.

Values are served in STORED form (what a ``ConfigSetting`` row or the TOML file holds),
not in the resolver's coerced dataclass form: the two TOML surfaces that consume this —
the export filters — write stored form, and the dashboard renders it as text.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from teatree.config.overlay_code_defaults import overlay_code_defaults
from teatree.config.override_read_health import SAFETY_FAIL_CLOSED_STORED_VALUES, ConfigOverrideReadError
from teatree.config.resolution import (
    NO_EFFECTIVE_DEFAULT,
    EnvOverrideRejection,
    effective_default,
    read_env_setting_overrides,
    read_setting_layers,
)

logger = logging.getLogger(__name__)

#: Never equals a real value, so "this tier has no opinion" is distinguishable from a
#: tier that genuinely stores ``None``.
_ABSENT: Any = object()


class ValueSource(StrEnum):
    """The tier that supplied a setting's effective value, highest precedence first.

    :attr:`UNRESOLVED` is not a tier — it is the honest answer when the DB override tier
    could not be READ (#3873). Crediting the shipped file in that state would name a tier
    that was never consulted, which is the same "partial failure presenting as a definite
    answer" the KIND column used to produce.

    :attr:`INVALID_ENV` is the same honesty one tier up: a ``T3_*`` var whose value the
    setting's parser refuses makes the resolver RAISE, so no tier below it is in force
    either. Naming the var and showing its raw value is what lets this page report the
    misconfiguration it exists to surface, instead of dying on it.
    """

    ENV = "env"
    INVALID_ENV = "env (value refused by the parser)"
    DB_OVERLAY = "DB overlay scope"
    DB_GLOBAL = "DB global scope"
    OVERLAY_CODE_DEFAULT = "overlay code default"
    CODE_DEFAULT = "code default"
    UNRESOLVED = "unresolved (DB override read failed)"


#: The tiers whose value an operator changed — as opposed to one teatree ships.
OVERRIDING_SOURCES: frozenset[ValueSource] = frozenset(
    {ValueSource.ENV, ValueSource.INVALID_ENV, ValueSource.DB_OVERLAY, ValueSource.DB_GLOBAL}
)


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """One setting's effective value in stored form, and the tier that supplied it."""

    key: str
    value: Any
    source: ValueSource

    @property
    def is_overridden(self) -> bool:
        """Whether an operator's own tier supplied the value, rather than a shipped one."""
        return self.source in OVERRIDING_SOURCES


@dataclass(frozen=True, slots=True)
class _Tiers:
    """The stored-form tiers of one scope's resolution, read once and walked per key."""

    env: Mapping[str, Any]
    db_overlay: Mapping[str, Any]
    db_global: Mapping[str, Any]
    code_default: Mapping[str, Any]
    #: True when a DB scope's read FAILED (#3873) — the tiers below ``env`` are then
    #: unknown, not absent, and naming one of them would credit a tier never consulted.
    degraded: bool = False
    #: The ``T3_*`` vars whose exported value the setting's parser refused. Highest
    #: precedence: the resolver raises on them, so nothing below is in force either.
    rejected_env: Mapping[str, EnvOverrideRejection] = MappingProxyType({})

    def resolve(self, key: str) -> ResolvedSetting:
        rejection = self.rejected_env.get(key)
        if rejection is not None:
            return ResolvedSetting(key, rejection.raw, ValueSource.INVALID_ENV)
        if self.degraded:
            env_value = self.env.get(key, _ABSENT)
            if env_value is not _ABSENT:
                return ResolvedSetting(key, env_value, ValueSource.ENV)
            return ResolvedSetting(key, _unresolved_value(key), ValueSource.UNRESOLVED)
        for source, tier in (
            (ValueSource.ENV, self.env),
            (ValueSource.DB_OVERLAY, self.db_overlay),
            (ValueSource.DB_GLOBAL, self.db_global),
            (ValueSource.OVERLAY_CODE_DEFAULT, self.code_default),
        ):
            value = tier.get(key, _ABSENT)
            if value is not _ABSENT:
                return ResolvedSetting(key, value, source)
        return ResolvedSetting(key, _declared_default(key), ValueSource.CODE_DEFAULT)


def _declared_default(key: str) -> object:
    """*key*'s shipped value in stored form, from the ONE default authority.

    A cold / cold-hook / registry key states its default on its registry entry and has no
    ``UserSettings`` field at all, so a dataclass-only read answers ``None`` for every one
    of them — which the dashboard renders as a blank gate and the export cannot render.
    """
    declared = effective_default(key)
    return None if declared is NO_EFFECTIVE_DEFAULT else declared


def _unresolved_value(key: str) -> object:
    """The value the RESOLVER will actually use for *key* while the tier is degraded.

    Read off the same fail-closed table ``resolution.fail_closed_overrides`` applies, so
    the value shown and the value in force cannot disagree — the property this whole
    module exists to hold. A key with no fail-closed entry keeps its declared default; it
    is reported ``UNRESOLVED`` all the same, because whether a row would have overridden
    it is exactly what could not be determined.
    """
    fail_closed = SAFETY_FAIL_CLOSED_STORED_VALUES.get(key, _ABSENT)
    if fail_closed is not _ABSENT:
        return fail_closed
    return _declared_default(key)


def _tiers(scope: str, *, persisted_only: bool) -> _Tiers:
    layers = read_setting_layers(scope)
    global_rows, overlay_rows = layers.db_rows
    # The NON-raising env read: a var whose value the parser refuses is the misconfiguration
    # an operator opens this page to find, and `env_setting_overrides` would take the page
    # down with it. `persisted_only` empties the tier anyway, so the refusals go with it.
    env = read_env_setting_overrides()
    return _Tiers(
        env={} if persisted_only else env.values,
        db_overlay=overlay_rows,
        db_global=global_rows,
        code_default={} if persisted_only else overlay_code_defaults(scope),
        degraded=bool(layers.degraded_scopes),
        rejected_env={} if persisted_only else env.rejected,
    )


def resolve_settings(
    keys: Iterable[str], *, scope: str = "", persisted_only: bool = False
) -> dict[str, ResolvedSetting]:
    """Each key's effective value and the tier it came from, for the *scope* being viewed.

    *scope* names the overlay whose ``ConfigSetting`` rows layer on top of the global ones
    — ``""`` is the global view, where only the global rows apply.

    *persisted_only* empties the two tiers a TOML file cannot express: ``env`` is process
    state and an overlay code default is the overlay's own constant, so baking either into
    a file every fresh install reads would change what those installs do. A file export
    takes it; the dashboard does NOT — an operator staring at a row needs to be told when
    ``env`` is what is actually winning.

    The tiers are read once for the whole call, so a page of two hundred rows costs one
    settings read rather than two hundred.

    Raises :class:`ConfigOverrideReadError` when *persisted_only* is set and the DB tier
    could not be read (#3873). A file export writes what it believes the stored tiers hold;
    doing that from a tier it could not read would persist an absence it never verified —
    turning a transient read fault into a permanent, silent config loss. The dashboard path
    (``persisted_only=False``) does NOT raise: it renders the degradation as
    :attr:`ValueSource.UNRESOLVED`, which is the whole point of showing it.
    """
    tiers = _tiers(scope, persisted_only=persisted_only)
    if persisted_only and tiers.degraded:
        raise ConfigOverrideReadError(scope)
    return {key: tiers.resolve(key) for key in keys}


__all__ = [
    "OVERRIDING_SOURCES",
    "ConfigOverrideReadError",
    "ResolvedSetting",
    "ValueSource",
    "resolve_settings",
]
