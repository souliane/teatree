"""Read the DB-home ``loops`` setting (its global + per-loop tables) (#1432, #1434, #2702).

The orchestrator gates every tick through :class:`LoopsConfig`. The ``loops``
setting is a dict: its top-level keys carry ``default_cadence`` / ``parallel`` /
``summary_dm``, and a nested ``<name>`` table carries a per-loop ``cadence``
override. Cadence values accept the suffix shorthand (``"30s"``, ``"5m"``,
``"1h"``) or a bare int. Floor 60 seconds — sub-minute cadences turn the tick
into a fetch storm. Bad values silently fall back to the default + one ERROR
log; never raise (a broken cadence string must not take down the loop).

Loop-disabled state is DB-only: :meth:`LoopsConfig.is_enabled` resolves
purely through the durable DB ``LoopState`` control tier (#1913) — a
``PAUSED`` / ``DISABLED`` row skips the loop, an absent / ``ENABLED`` row
leaves it running. There is no env kill-switch and no ``loops``-config
disabled-state fallback: loop control is ``/loops``
(``t3 loop enable``/``disable``/``pause``/``resume``) + the DB only.
"""

import dataclasses
import logging
from pathlib import Path
from typing import Any

from teatree.loop.loop_state_db import loop_held_in_db
from teatree.loops.base import MiniLoop

logger = logging.getLogger(__name__)

_CADENCE_FLOOR = 60
_DEFAULT_CADENCE = 300  # 5 minutes — matches the legacy fat-loop cadence floor.


def parse_cadence(raw: object, *, default: int) -> int:
    """Parse ``"30s"`` / ``"5m"`` / ``"1h"`` / int into seconds.

    Floor at 60s. Bad value → *default* + ERROR log; never raises.
    """
    if raw is None:
        return default
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(raw, _CADENCE_FLOOR)
    if not isinstance(raw, str):
        logger.error("Cadence value of unsupported type %r — falling back to %ds", type(raw).__name__, default)
        return default
    return _parse_cadence_str(raw, default=default)


def _parse_cadence_str(raw: str, *, default: int) -> int:
    """Parse the string-suffix variant of a cadence value."""
    s = raw.strip().lower()
    if not s:
        return default
    if s.isdigit():
        return max(int(s), _CADENCE_FLOOR)
    body, unit = s[:-1], s[-1]
    if not body.isdigit():
        logger.error("Bad cadence value %r — falling back to %ds", raw, default)
        return default
    multiplier = {"s": 1, "m": 60, "h": 3600}.get(unit)
    if multiplier is None:
        logger.error("Bad cadence unit %r — falling back to %ds", raw, default)
        return default
    return max(int(body) * multiplier, _CADENCE_FLOOR)


@dataclasses.dataclass(frozen=True, slots=True)
class LoopOverride:
    """Per-loop cadence override under the ``loops`` setting's ``<name>`` table.

    ``None`` means "no override; fall back to the loop's own default
    cadence". The disabled decision is NOT a per-loop config override: it
    resolves through the DB ``LoopState`` tier only (see
    :meth:`LoopsConfig.is_enabled`).
    """

    cadence_seconds: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class LoopsConfig:
    """Loop-orchestration config — cadence/parallel/summary defaults + per-loop cadence overrides."""

    default_cadence: int = _DEFAULT_CADENCE
    parallel: bool = True
    summary_dm: str = "errors"  # never | errors | always
    per_loop: dict[str, LoopOverride] = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls, db_path: Path | None = None) -> "LoopsConfig":
        """Read the DB-home ``loops`` setting into a :class:`LoopsConfig`.

        *db_path* override is for tests. An absent ``loops`` row (or no DB)
        degrades to defaults — never raise. Only cadence/parallel/summary
        settings are read; loop-disabled state is resolved by
        :meth:`is_enabled` (DB → default), not here.
        """
        from teatree.config import cold_reader  # noqa: PLC0415

        return cls._from_table(cold_reader.mapping_setting("loops", db_path=db_path))

    @classmethod
    def _from_table(cls, table: dict[str, Any]) -> "LoopsConfig":
        parallel = bool(table.get("parallel", True))
        summary_dm = str(table.get("summary_dm", "errors"))
        default_cadence = parse_cadence(table.get("default_cadence"), default=_DEFAULT_CADENCE)
        per_loop: dict[str, LoopOverride] = {}
        for key, value in table.items():
            if not isinstance(value, dict):
                continue
            per_loop[key] = LoopOverride(cadence_seconds=_parse_per_loop_cadence(value.get("cadence")))
        return cls(
            default_cadence=default_cadence,
            parallel=parallel,
            summary_dm=summary_dm,
            per_loop=per_loop,
        )

    @staticmethod
    def is_enabled(loop: MiniLoop) -> bool:
        """Resolve enable/disable for *loop* through the DB ``LoopState`` tier only (#1913).

        The durable DB-backed ``LoopState`` control tier is the single disable
        authority: a ``PAUSED`` / ``DISABLED`` row forces a skip, while no row
        (or an ``ENABLED`` row) leaves the loop running, so an empty table is a
        provable no-op. There is no env kill-switch and no ``loops``-config
        disabled-state fallback — loop control is ``t3 loop enable`` /
        ``disable`` / ``pause`` / ``resume`` + the DB only. The decision reads no
        config field, so it is a static method.
        """
        return not loop_held_in_db(loop.name)

    def cadence_for(self, loop: MiniLoop) -> int:
        """Resolve effective cadence (seconds) for *loop*."""
        override = self.per_loop.get(loop.name)
        if override is not None and override.cadence_seconds is not None:
            return override.cadence_seconds
        return loop.default_cadence_seconds


def _parse_per_loop_cadence(raw: object) -> int | None:
    """Per-loop cadence parser — None on absence OR bad value.

    Differs from :func:`parse_cadence` (used for the global default) by
    degrading bad values to ``None`` so the loop's intrinsic default
    cadence wins instead of clamping to the global default.
    """
    if raw is None:
        return None
    # Validate via the strict parser; if it falls back to the sentinel
    # we passed in, the value was bad and we degrade to None.
    sentinel = -1
    parsed = parse_cadence(raw, default=sentinel)
    if parsed == sentinel:
        return None
    return parsed
