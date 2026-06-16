"""Layer-neutral mini-loop enable resolution by loop NAME (#79).

The single source of the env/toml loop-enable doctrine (env kill-switch →
per-loop table → global), housed in a leaf module both layers can import.
:class:`teatree.loops.config.LoopsConfig` has the :class:`MiniLoop` object and
resolves enable/cadence for the orchestrator and live-tick fan-out. The
review-claim chokepoint in :mod:`teatree.loop` only knows the loop NAME and must
not import :mod:`teatree.loops` (a forbidden up-stack dependency), so it reaches
an identical env/toml verdict through this module. Keeping the resolution here —
not duplicated in each consumer — means the "is the review loop stopped?" answer
cannot drift between the fan-out gate and the claim gate.

The DB-backed ``LoopState`` control tier (#1913) sits ABOVE this primitive but
cannot live in it: this is a ``platform``-layer leaf and the ORM is a ``domain``
layer, so reading the DB here would be a backwards tach edge. The DB tier is
applied by each caller that may legally read the ORM — the tick via
:meth:`teatree.loops.config.LoopsConfig.is_enabled` and the review-claim
chokepoint via :func:`teatree.loop.loop_state_db.loop_held_in_db`, both reading
the single :class:`teatree.core.models.LoopState` model.
"""

import os
import tomllib
from pathlib import Path

import teatree.config as _config


def loop_enabled_by_name(name: str, *, always_on: bool = False, path: Path | None = None) -> bool:
    """Resolve a mini-loop's env/toml enable state by NAME.

    First match wins:

    1. ``T3_LOOPS_DISABLED`` env (``"all"`` sentinel, or a comma list of
        names) — a hard kill-switch ignored only by an ``always_on`` loop.
    2. ``[loops.<name>] enabled`` per-loop override.
    3. ``[loops] enabled`` global (default ``True``).

    Fail-safe: any read error returns ``True`` — an unreadable config must
    never silently disable a loop. The DB ``LoopState`` tier (#1913) is layered
    ON TOP by the caller (see the module docstring), not here.
    """
    env_disabled = _loops_disabled_env()
    if not always_on and ("all" in env_disabled or name in env_disabled):
        return False
    if always_on:
        return True
    loops_table = _loops_table(path)
    per_loop = loops_table.get(name)
    if isinstance(per_loop, dict) and isinstance(per_loop.get("enabled"), bool):
        return per_loop["enabled"]
    return bool(loops_table.get("enabled", True))


def _loops_table(path: Path | None) -> dict:
    """Return the ``[loops]`` table from the config, or ``{}`` on any read error."""
    # Resolve the config path through the ``teatree.config`` facade at call time
    # (mirroring ``loader.load_config``'s ``_facade.CONFIG_PATH`` indirection)
    # rather than a frozen module-level import — a frozen binding ignores a
    # ``config.CONFIG_PATH`` redirect (e.g. the test-isolation fixture pointing
    # at a hermetic ``~/.teatree.toml``), so the host's real ``[loops.review]
    # enabled = false`` leaked into the suite and dropped review-intent signals.
    toml_path = path if path is not None else _config.CONFIG_PATH
    try:
        if not toml_path.is_file():
            return {}
        with toml_path.open("rb") as fh:
            table = tomllib.load(fh).get("loops", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return table if isinstance(table, dict) else {}


def _loops_disabled_env() -> frozenset[str]:
    raw = os.environ.get("T3_LOOPS_DISABLED", "").strip()
    if not raw:
        return frozenset()
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    if "all" in {p.lower() for p in parts}:
        return frozenset({"all"})
    return frozenset(parts)


__all__ = ["loop_enabled_by_name"]
