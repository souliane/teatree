"""Read ``[loops]`` + ``[loops.<name>]`` from ``~/.teatree.toml`` (#1432, #1434).

The orchestrator gates every tick through :class:`LoopsConfig`. Three
layers, first match wins per setting: env override
(``T3_LOOPS_DISABLED=name1,name2`` or ``all`` — hard kill-switch
ignored only by :attr:`MiniLoop.always_on` loops), per-loop table
(``[loops.<name>]`` with ``enabled`` / ``cadence`` keys overriding the
globals), and global table (``[loops]`` with ``enabled`` /
``default_cadence`` / ``parallel`` / ``summary_dm`` keys).

Cadence values accept the suffix shorthand (``"30s"``, ``"5m"``,
``"1h"``) or a bare int. Floor 60 seconds — sub-minute cadences turn
the tick into a fetch storm. Bad values silently fall back to the
default + one ERROR log; never raise (a broken cadence string must not
take down the loop).
"""

import dataclasses
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

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
    """Per-loop overrides under ``[loops.<name>]``.

    ``None`` on a field means "no override; fall back to the global
    default for that field". An explicit bool/int overrides.
    """

    enabled: bool | None = None
    cadence_seconds: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class LoopsConfig:
    """Loop-orchestration config — defaults + per-loop overrides."""

    enabled: bool = True
    default_cadence: int = _DEFAULT_CADENCE
    parallel: bool = True
    summary_dm: str = "errors"  # never | errors | always
    per_loop: dict[str, LoopOverride] = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "LoopsConfig":
        """Read ``[loops]`` / ``[loops.<name>]`` from ``~/.teatree.toml``.

        *path* override is for tests. Missing file, missing tables, or
        unreadable toml all degrade to defaults — never raise.
        """
        toml_path = path if path is not None else Path.home() / ".teatree.toml"
        try:
            if not toml_path.is_file():
                return cls()
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            logger.exception("LoopsConfig.load failed reading %s", toml_path)
            return cls()

        loops_table = data.get("loops")
        if not isinstance(loops_table, dict):
            return cls()

        return cls._from_table(loops_table)

    @classmethod
    def _from_table(cls, table: dict[str, Any]) -> "LoopsConfig":
        enabled = bool(table.get("enabled", True))
        parallel = bool(table.get("parallel", True))
        summary_dm = str(table.get("summary_dm", "errors"))
        default_cadence = parse_cadence(table.get("default_cadence"), default=_DEFAULT_CADENCE)
        per_loop: dict[str, LoopOverride] = {}
        for key, value in table.items():
            if not isinstance(value, dict):
                continue
            per_loop[key] = LoopOverride(
                enabled=value.get("enabled") if isinstance(value.get("enabled"), bool) else None,
                cadence_seconds=_parse_per_loop_cadence(value.get("cadence")),
            )
        return cls(
            enabled=enabled,
            default_cadence=default_cadence,
            parallel=parallel,
            summary_dm=summary_dm,
            per_loop=per_loop,
        )

    def is_enabled(self, loop: MiniLoop) -> bool:
        """Resolve enable/disable for *loop* across env, per-loop, global, always-on.

        The env kill-switch is resolved against the shared
        :func:`teatree.config.loop_enabled_by_name`-style env parsing here
        (``_env_disabled_names`` keeps the case-insensitive ``all`` sentinel);
        the per-loop / global layers read from this already-parsed config so a
        ``LoopsConfig`` built from an explicit ``path`` (tests) stays
        authoritative.
        """
        env_disabled = _env_disabled_names()
        if env_disabled == _ENV_DISABLE_ALL and not loop.always_on:
            return False
        if loop.name in env_disabled and not loop.always_on:
            return False
        if loop.always_on:
            return True
        override = self.per_loop.get(loop.name)
        if override is not None and override.enabled is not None:
            return override.enabled
        return self.enabled

    def cadence_for(self, loop: MiniLoop) -> int:
        """Resolve effective cadence (seconds) for *loop*."""
        override = self.per_loop.get(loop.name)
        if override is not None and override.cadence_seconds is not None:
            return override.cadence_seconds
        return loop.default_cadence_seconds


_ENV_DISABLE_ALL = frozenset({"all"})


def _env_disabled_names() -> frozenset[str]:
    """Parse ``T3_LOOPS_DISABLED`` → frozenset of names.

    ``"all"`` is a single sentinel that means "every non-always-on loop
    is disabled". Whitespace tolerant; case-insensitive on the sentinel.
    """
    raw = os.environ.get("T3_LOOPS_DISABLED", "").strip()
    if not raw:
        return frozenset()
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    if "all" in {p.lower() for p in parts}:
        return _ENV_DISABLE_ALL
    return frozenset(parts)


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
