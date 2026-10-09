"""Shared signal-trust and budget guards for self-improvement loops."""

from dataclasses import dataclass
from datetime import datetime

from teatree.core.factory.factory_signals import FactorySignalsReport, SignalStatus, compute_factory_signals
from teatree.loop.self_improve.budget import BudgetVerdict

SIGNAL_UNTRUSTED = "signal_untrusted"
BUDGET = "budget"


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
class SignalTrust:
    """Whether every factory signal is trustworthy, naming any gap providers."""

    trusted: bool
    gap_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GuardSeams:
    """Injectable seams so each guard is exercisable in isolation.

    Each is ``None`` in production (the real probe applies); tests supply fakes so
    a guard can be driven without standing up factory-signal providers and a
    self-improve budget.
    """

    signal_report: FactorySignalsReport | None = None
    budget: BudgetVerdict | None = None


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
