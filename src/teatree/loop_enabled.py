"""Layer-neutral mini-loop enable resolution by loop NAME (#79, #2702).

The single source of the loop-enable doctrine for the ``platform`` layer: the
``T3_LOOPS_DISABLED`` env kill-switch resolved by loop NAME, housed in a leaf
module both layers can import. :class:`teatree.loops.config.LoopsConfig` has the
:class:`MiniLoop` object and resolves enable/cadence for the orchestrator and
live-tick fan-out. The review-claim chokepoint in :mod:`teatree.loop` only knows
the loop NAME and must not import :mod:`teatree.loops` (a forbidden up-stack
dependency), so it reaches an identical env verdict through this module. Keeping
the resolution here — not duplicated in each consumer — means the "is the review
loop stopped?" answer cannot drift between the fan-out gate and the claim gate.

Platform-leaf boundary (#2702). This module is a ``platform``-layer leaf, so it
stays env-only and DB-free on purpose. The ``T3_LOOPS_DISABLED`` env path is a
deliberate hard kill-switch that must work pre-Django (settled by #2359):
reading the env requires no process bootstrap, so the kill-switch holds even
before the ORM is configured — that is exactly why env stays env and is
resolved in this leaf. The DB-backed ``LoopState`` control tier (#1913) sits
ABOVE this primitive but cannot live in it: the ORM is a ``domain`` layer, so
reading the DB here would be a backwards tach edge. The DB tier is applied by
each caller that may legally read the ORM — the tick via
:meth:`teatree.loops.config.LoopsConfig.is_enabled` and the review-claim
chokepoint via :func:`teatree.loop.loop_state_db.loop_held_in_db`, both reading
the single :class:`teatree.core.models.LoopState` model.

The former ``[loops]`` toml fallback (#2697 audit bypass reader B6) is gone: the
disabled decision now resolves env (here) → DB ``LoopState`` (caller) → default,
with no ``tomllib.load()`` of the toml ``[loops]`` section.
"""

import os


def loop_enabled_by_name(name: str, *, always_on: bool = False) -> bool:
    """Resolve a mini-loop's env enable state by NAME.

    First match wins:

    1. ``T3_LOOPS_DISABLED`` env (``"all"`` sentinel, or a comma list of
        names) — a hard kill-switch ignored only by an ``always_on`` loop.
    2. Default ``True`` — an absent kill-switch leaves the loop runnable.

    The disabled-state doctrine is env (here) → DB ``LoopState`` → default; the
    DB ``LoopState`` tier (#1913) is layered ON TOP by the caller (see the module
    docstring), not here, since this is a DB-free platform leaf. The legacy
    ``[loops]`` toml fallback was removed in #2702.
    """
    env_disabled = _loops_disabled_env()
    env_kills = "all" in env_disabled or name in env_disabled
    return not (env_kills and not always_on)


def _loops_disabled_env() -> frozenset[str]:
    raw = os.environ.get("T3_LOOPS_DISABLED", "").strip()
    if not raw:
        return frozenset()
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    if "all" in {p.lower() for p in parts}:
        return frozenset({"all"})
    return frozenset(parts)


__all__ = ["loop_enabled_by_name"]
