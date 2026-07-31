"""Where a setting's effective value actually CAME FROM — the resolution chain, named.

A settings surface can show a value; only provenance answers the operator's real
question, "why is it that". The dashboard used to answer it with the setting's KIND
(``default`` / ``personal`` / ``secret``), which reads ``default`` for hundreds of
consecutive rows and, sitting beside a *shipped default* column, is misread as "this
value came from the default" — while the row right beside it says the value differs
from that default.

There is exactly ONE resolution here: the tiers are read through
:func:`~teatree.config.resolution.read_setting_layers` and
:func:`~teatree.config.resolution.env_setting_overrides`, the same seams
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
from typing import Any

from teatree.config.overlay_code_defaults import overlay_code_defaults
from teatree.config.override_read_health import SAFETY_FAIL_CLOSED_STORED_VALUES, ConfigOverrideReadError
from teatree.config.resolution import env_setting_overrides, read_setting_layers
from teatree.config.settings import UserSettings

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
    """

    ENV = "env"
    DB_OVERLAY = "DB overlay scope"
    DB_GLOBAL = "DB global scope"
    OVERLAY_CODE_DEFAULT = "overlay code default"
    SHIPPED_FILE = "shipped file"
    CODE_DEFAULT = "code default"
    UNRESOLVED = "unresolved (DB override read failed)"


#: The tiers whose value an operator changed — as opposed to one teatree ships.
OVERRIDING_SOURCES: frozenset[ValueSource] = frozenset({ValueSource.ENV, ValueSource.DB_OVERLAY, ValueSource.DB_GLOBAL})

#: The tiers a TOML file can express. ``env`` is process state and an overlay code default
#: is the overlay's own constant: writing either into a shipped file would bake a
#: machine-local or overlay-local decision into what every fresh install starts from.
PERSISTED_SOURCES: frozenset[ValueSource] = frozenset(
    {ValueSource.DB_OVERLAY, ValueSource.DB_GLOBAL, ValueSource.SHIPPED_FILE}
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
    shipped_file: Mapping[str, Any]
    #: True when a DB scope's read FAILED (#3873) — the tiers below ``env`` are then
    #: unknown, not absent, and naming one of them would credit a tier never consulted.
    degraded: bool = False

    def resolve(self, key: str) -> ResolvedSetting:
        if self.degraded:
            env_value = self.env.get(key, _ABSENT)
            if env_value is not _ABSENT:
                return ResolvedSetting(key, env_value, ValueSource.ENV)
            return ResolvedSetting(key, self._unresolved_value(key), ValueSource.UNRESOLVED)
        for source, tier in (
            (ValueSource.ENV, self.env),
            (ValueSource.DB_OVERLAY, self.db_overlay),
            (ValueSource.DB_GLOBAL, self.db_global),
            (ValueSource.OVERLAY_CODE_DEFAULT, self.code_default),
            (ValueSource.SHIPPED_FILE, self.shipped_file),
        ):
            value = tier.get(key, _ABSENT)
            if value is not _ABSENT:
                return ResolvedSetting(key, value, source)
        return ResolvedSetting(key, getattr(UserSettings(), key, None), ValueSource.CODE_DEFAULT)

    def _unresolved_value(self, key: str) -> object:
        """The value the RESOLVER will actually use for *key* while the tier is degraded.

        Read off the same fail-closed table ``resolution.fail_closed_overrides`` applies, so
        the value shown and the value in force cannot disagree — the property this whole
        module exists to hold. A key with no fail-closed entry keeps its shipped value; it
        is reported ``UNRESOLVED`` all the same, because whether a row would have overridden
        it is exactly what could not be determined.
        """
        fail_closed = SAFETY_FAIL_CLOSED_STORED_VALUES.get(key, _ABSENT)
        if fail_closed is not _ABSENT:
            return fail_closed
        shipped = self.shipped_file.get(key, _ABSENT)
        if shipped is not _ABSENT:
            return shipped
        return getattr(UserSettings(), key, None)


def _tiers(scope: str, *, persisted_only: bool) -> _Tiers:
    layers = read_setting_layers(scope)
    global_rows, overlay_rows = layers.db_rows
    return _Tiers(
        env={} if persisted_only else env_setting_overrides(),
        db_overlay=overlay_rows,
        db_global=global_rows,
        code_default={} if persisted_only else overlay_code_defaults(scope),
        shipped_file=layers.toml_rows,
        degraded=bool(layers.degraded_scopes),
    )


def resolve_settings(
    keys: Iterable[str], *, scope: str = "", persisted_only: bool = False
) -> dict[str, ResolvedSetting]:
    """Each key's effective value and the tier it came from, for the *scope* being viewed.

    *scope* names the overlay whose ``ConfigSetting`` rows layer on top of the global ones
    — ``""`` is the global view, where only the global rows apply.

    *persisted_only* restricts the walk to :data:`PERSISTED_SOURCES`, the tiers a TOML file
    can express. A file export takes it: a value that reached this process through ``T3_*``
    or through the active overlay's own constants is machine-local or overlay-local, and
    baking it into a file every fresh install reads would change what those installs do.
    The dashboard does NOT take it — an operator staring at a row needs to be told when
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
    "PERSISTED_SOURCES",
    "ConfigOverrideReadError",
    "ResolvedSetting",
    "ValueSource",
    "resolve_settings",
]
