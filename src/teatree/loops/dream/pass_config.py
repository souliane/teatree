"""The ``[loops.dream]`` table and the per-pass promotion policy read from it (#4176).

`loop.py` owns the ten phase kill-switches — booleans answering "does phase P run at
all?". The two settings here answer a different question: given that a phase DOES run,
how may it promote? ``promotion_cap`` bounds how many gaps ONE pass schedules;
``validate_live`` decides whether that pass's eval promotion runs the METERED live
validator. Neither turns a phase on or off, so neither belongs with the kill-switches.

Every promoting phase — core-gap memory promotion (Pass 2), the automatable-ask promoter
(3d), and compliance escalation (3c) — drives its gaps through the single
:func:`teatree.loops.dream.umbrella_ledger.promote_gap` chokepoint, which schedules a
real coding task per gap. That chokepoint was unbounded, so the FIRST pass with those
default-OFF toggles on would dump the whole waiting backlog in one night (measured
2026-08-04: 62 CORE_GAP rows).

One budget is built per pass and shared by all three phases, so the cap is per-PASS
rather than per-phase — three phases each granted the full cap would triple it. Budget
is spent only when a promotion did NEW work, so an already-promoted gap re-visited on a
later pass is free and cannot starve fresh gaps out of a steady-state backlog. Deferred
gaps are never dropped: they stay in their phase's drain queue and the next pass picks
them up.
"""

import os
from dataclasses import dataclass

#: Recognised explicit env values; anything else defers to the DB key.
TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off"})

_VALIDATE_LIVE_ENV = "T3_DREAM_VALIDATE_LIVE"
_PROMOTION_CAP_ENV = "T3_DREAM_PROMOTION_CAP"
_DEFAULT_PROMOTION_CAP = 5


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


def promotion_cap() -> int:
    """The per-pass gap-promotion cap (default 5; ``0`` ⇒ unbounded), env then DB key.

    A negative or unparsable value degrades to the default rather than raising, so a
    mistyped setting can never turn the bound off — the same fail-safe direction
    :func:`teatree.config.cold_reader.int_setting` takes.
    """
    raw_env = os.environ.get(_PROMOTION_CAP_ENV, "").strip()
    if raw_env:
        try:
            from_env = int(raw_env)
        except ValueError:
            from_env = -1
        if from_env >= 0:
            return from_env
    stored = dream_table().get("promotion_cap")
    if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0:
        return stored
    return _DEFAULT_PROMOTION_CAP


@dataclass(slots=True)
class PromotionBudget:
    """One pass's remaining gap-promotion allowance; ``remaining=None`` is unbounded."""

    remaining: int | None
    deferred: int = 0

    @classmethod
    def from_config(cls) -> "PromotionBudget":
        """Build this pass's budget from the ``promotion_cap`` setting (``0`` ⇒ unbounded)."""
        cap = promotion_cap()
        return cls(remaining=None if cap <= 0 else cap)

    @property
    def exhausted(self) -> bool:
        """Whether this pass has spent its allowance and must defer further gaps."""
        return self.remaining is not None and self.remaining <= 0

    def spend(self) -> None:
        """Charge one NEW promotion against the allowance."""
        if self.remaining is not None:
            self.remaining -= 1

    def defer(self) -> None:
        """Record one gap the cap turned away, for the pass summary."""
        self.deferred += 1

    @property
    def summary(self) -> str:
        """The pass-summary clause naming what the cap deferred — a silent cap reads as 'all done'."""
        if not self.deferred:
            return ""
        return f"; deferred {self.deferred} promotion(s) over the per-pass cap — the next pass drains them"


__all__ = ["FALSY", "TRUTHY", "PromotionBudget", "dream_table", "promotion_cap", "validate_live_enabled"]
