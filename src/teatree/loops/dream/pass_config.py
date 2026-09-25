"""The ``[loops.dream]`` table and the per-pass WALL-CLOCK budget read from it (#4776).

`loop.py` owns the ten phase kill-switches — booleans answering "does phase P run at
all?". ``validate_live_enabled`` and :class:`PassBudget` answer a different question:
given that a phase DOES run, how much may it spend? ``validate_live`` decides whether a
pass's eval promotion runs the METERED live validator; :class:`PassBudget` bounds how
LONG the pass spends before it must stop starting new metered work.

There used to be a second budget here — :class:`PromotionBudget`, which rationed how
MANY gaps one pass could schedule as its own coding task, because every promoting phase
(core-gap memory promotion, the automatable-ask promoter, compliance escalation) drove
each gap through its own ``schedule_coding()`` call: one gap, one ticket, one PR. That
was the wrong fix for the wrong problem — the fan-out itself was the defect, not its
rate — so #4776 deleted the ration and replaced the fan-out with
:mod:`teatree.loops.dream.batch_promote`: every gap a pass decides to promote collects
into ONE :class:`~teatree.loops.dream.batch_promote.PromotionBatch` and mints AT MOST
ONE ticket, so there is nothing left to ration.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

#: Recognised explicit env values; anything else defers to the DB key.
TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off"})

_VALIDATE_LIVE_ENV = "T3_DREAM_VALIDATE_LIVE"


def dream_table() -> dict:
    """The ``dream`` sub-table of the DB ``loops`` setting; ``{}`` on absence/failure."""
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: loaded at tick time, not import

    dream = cold_reader.mapping_setting("loops").get("dream")
    return dream if isinstance(dream, dict) else {}


def validate_live_enabled() -> bool:
    """Whether eval promotion runs the METERED live validator (default OFF, #4176).

    Default OFF is the nightly tick's key safety property — without the validator every
    clearing candidate is WITHHELD. It had no config path at all (only ``--full`` /
    ``--validate-live``), so the cron pass could never reach live promotion however it
    was configured; this is that path.
    """
    raw_env = os.environ.get(_VALIDATE_LIVE_ENV, "").strip().lower()
    if raw_env in TRUTHY:
        return True
    if raw_env in FALSY:
        return False
    stored = dream_table().get("validate_live")
    return stored if isinstance(stored, bool) else False


@dataclass(frozen=True, slots=True)
class PassBudget:
    """One dream pass's WALL-CLOCK allowance, and the reserve its TAIL is owed.

    The pass had a budget constant from the start (``DREAM_PASS_BUDGET_SECONDS``) and it
    was dead: it sized the lease TTL and nothing else, because nothing inside the pass
    ever read a clock. The only thing that ever ended a pass was the driver's SIGKILL at
    an EQUAL deadline — so the pass always died mid-distil and every phase after the
    distiller (compliance, the §4 acceptance gates, phases 4-6, Pass-2 promotion, the
    marker) was unreachable. This makes the constant load-bearing.

    ``tail_reserve`` is what the pass keeps back for everything after the distiller.
    ``allows_new_call(cost)`` is the whole enforcement: a new metered call is launched
    only when the budget can absorb its worst case AND still leave the reserve intact.
    Stopping the walk early does not DROP the un-reached corpus — the distiller's
    rotation cursor carries it to the next pass, exactly as the per-pass batch cap
    already did.

    *clock* is injected so a test can drive a pass to its ceiling without sleeping.
    """

    started_at: float
    total: float
    tail_reserve: float
    clock: Callable[[], float] = field(default=time.monotonic)

    @classmethod
    def start(cls, *, total: float, tail_reserve: float, clock: Callable[[], float] = time.monotonic) -> "PassBudget":
        """Open a budget anchored at *clock* now."""
        return cls(started_at=clock(), total=total, tail_reserve=tail_reserve, clock=clock)

    @property
    def elapsed(self) -> float:
        """Seconds spent since the pass opened its budget."""
        return self.clock() - self.started_at

    @property
    def remaining(self) -> float:
        """Seconds of budget left — negative once the pass is over its allowance."""
        return self.total - self.elapsed

    def allows_new_call(self, cost: float) -> bool:
        """Whether a call whose worst case is *cost* fits AND still leaves the tail its reserve.

        *cost* is the callee's own watchdog, not an estimate: a distiller call is bounded
        by :data:`teatree.loops.dream.sdk_distiller.DISTILL_WATCHDOG_SECONDS`, so a call
        launched with less than ``tail_reserve + cost`` left can, in its worst case, eat
        the reserve — which is exactly the state that made the tail unreachable.
        """
        return self.remaining >= self.tail_reserve + cost


__all__ = [
    "FALSY",
    "TRUTHY",
    "PassBudget",
    "dream_table",
    "validate_live_enabled",
]
