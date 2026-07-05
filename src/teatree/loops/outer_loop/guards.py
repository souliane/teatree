"""The unconditional guard chain — the code half of the QUADRUPLE-OFF (T4-PR-3).

Every outer-loop tick runs :func:`evaluate_guards` before it touches an
experiment. The chain is fail-closed and ordered so the first (most fundamental)
refusal wins. G1 flag: ``outer_loop_enabled`` off ⇒ ``outer_loop_disabled``. G2
critic-live: the critic gate is not registered, or has fewer than
:data:`MIN_CRITIC_SAMPLE` ``CriticVerdict`` rows ⇒ ``critic_not_live`` — this is
CODE not config, :func:`probe_critic_liveness` does a defensive lazy lookup and
fails CLOSED when it cannot CONFIRM a live critic (never optimise merges an
unproven supervisor cannot block). G3 signal-trust: any factory signal reports
``instrumentation_gap`` ⇒ ``signal_untrusted`` (never optimise an untrustworthy
score). G4 budget: the shared self-improve :func:`precheck_budget` refuses ⇒
``budget:<reason>``.

:func:`admission_verdict` is the SEPARATE new-proposal bound (max-concurrent,
weekly cap, convergence brake) the tick consults only when it would PROPOSE — an
in-flight experiment is still advanced past these caps. Both return a typed
:class:`GuardVerdict`; nothing here mutates state, so every guard is table-tested.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from teatree.core import models as core_models
from teatree.core.factory_signals import FactorySignalsReport, SignalStatus, compute_factory_signals
from teatree.core.models import OuterLoopExperiment
from teatree.loop.self_improve.budget import BudgetVerdict, precheck_budget

FLAG_OFF = "outer_loop_disabled"
CRITIC_NOT_LIVE = "critic_not_live"
SIGNAL_UNTRUSTED = "signal_untrusted"
BUDGET = "budget"
CONCURRENCY_CAP = "concurrency_cap"
WEEKLY_CAP = "weekly_cap"
CONVERGED = "converged"

#: The critic must have produced at least this many ``CriticVerdict`` rows before
#: G2 treats it as a live, non-vacuous merge supervisor.
MIN_CRITIC_SAMPLE = 5


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """One guard-chain outcome: ``ok`` plus the refusal ``reason`` (empty when ok)."""

    ok: bool
    reason: str = ""

    @classmethod
    def refuse(cls, reason: str) -> "GuardVerdict":
        return cls(ok=False, reason=reason)

    @classmethod
    def allow(cls) -> "GuardVerdict":
        return cls(ok=True, reason="")


@dataclass(frozen=True, slots=True)
class CriticLiveness:
    """Whether the merge-supervising critic is live, and its sample size."""

    live: bool
    verdict_count: int


@dataclass(frozen=True, slots=True)
class SignalTrust:
    """Whether every factory signal is trustworthy, naming any gap providers."""

    trusted: bool
    gap_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GuardSeams:
    """Injectable seams so the guard chain is exercisable without a live critic.

    Each is ``None`` in production (the real probe applies); tests supply fakes to
    drive the positive path the shipped state cannot reach yet.
    """

    critic_probe: Callable[[], CriticLiveness] | None = None
    signal_report: FactorySignalsReport | None = None
    budget: BudgetVerdict | None = None


class OuterLoopSettings(Protocol):
    """The effective-settings surface the outer loop reads.

    Structural, so a real ``UserSettings`` and a test ``SimpleNamespace`` both
    satisfy it without an explicit inheritance.
    """

    outer_loop_enabled: bool
    outer_loop_measure_days: int
    outer_loop_max_per_week: int
    outer_loop_stop_after_consecutive_failures: int


def probe_critic_liveness() -> CriticLiveness:
    """Defensive lazy lookup of the critic gate — fail CLOSED when unconfirmable.

    The sibling self-catching critic PR registers a ``CriticVerdict`` model and a
    ``critic`` gate. Until then (and on ANY lookup failure) this reports NOT live,
    so G2 refuses every tick — the intended shipped state. When the critic lands,
    the same probe reports live once it has ``>= MIN_CRITIC_SAMPLE`` verdict rows.
    """
    model = getattr(core_models, "CriticVerdict", None)
    if model is None:
        return CriticLiveness(live=False, verdict_count=0)
    try:
        count = int(model.objects.count())
    except Exception:  # noqa: BLE001 — a broken count query means we cannot confirm the critic
        return CriticLiveness(live=False, verdict_count=0)
    return CriticLiveness(live=count >= MIN_CRITIC_SAMPLE, verdict_count=count)


def probe_signal_trust(
    *,
    overlay: str = "",
    now: datetime | None = None,
    report: FactorySignalsReport | None = None,
) -> SignalTrust:
    """Trusted iff NO factory signal reports ``instrumentation_gap``."""
    resolved = report if report is not None else compute_factory_signals(overlay=overlay, now=now)
    gaps = tuple(row.provider_id for row in resolved.signals if row.reading.status == SignalStatus.INSTRUMENTATION_GAP)
    return SignalTrust(trusted=not gaps, gap_ids=gaps)


def evaluate_guards(
    *,
    settings: OuterLoopSettings,
    seams: GuardSeams | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Run G1→G2→G3→G4; return the first refusal, else allow."""
    resolved = seams or GuardSeams()
    if not settings.outer_loop_enabled:
        return GuardVerdict.refuse(FLAG_OFF)
    critic = (resolved.critic_probe or probe_critic_liveness)()
    if not critic.live:
        return GuardVerdict.refuse(CRITIC_NOT_LIVE)
    trust = probe_signal_trust(overlay=overlay, now=now, report=resolved.signal_report)
    if not trust.trusted:
        return GuardVerdict.refuse(SIGNAL_UNTRUSTED)
    resolved_budget = resolved.budget if resolved.budget is not None else precheck_budget()
    if not resolved_budget.ok:
        return GuardVerdict.refuse(f"{BUDGET}:{resolved_budget.reason}")
    return GuardVerdict.allow()


def admission_verdict(
    *,
    settings: OuterLoopSettings,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Whether a NEW proposal is admissible: concurrency → convergence → weekly.

    Consulted only when the tick would PROPOSE — an in-flight experiment is
    advanced regardless. ``converged`` (the human-attention brake) outranks the
    weekly cap (a mere wait): a loop that failed N times in a row should PARK and
    surface to a human, not silently idle out its weekly budget.
    """
    if OuterLoopExperiment.objects.active_count(overlay=overlay) >= 1:
        return GuardVerdict.refuse(CONCURRENCY_CAP)
    consecutive = OuterLoopExperiment.objects.consecutive_non_kept(overlay=overlay)
    if consecutive >= settings.outer_loop_stop_after_consecutive_failures:
        return GuardVerdict.refuse(CONVERGED)
    if OuterLoopExperiment.objects.weekly_count(overlay=overlay, now=now) >= settings.outer_loop_max_per_week:
        return GuardVerdict.refuse(WEEKLY_CAP)
    return GuardVerdict.allow()
