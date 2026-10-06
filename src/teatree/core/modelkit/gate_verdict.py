"""Gate verdicts bound to evidence that was actually read: a fact nobody read is Unknown, never a pass.

:func:`evaluate_gate` collects a gate's evidence, then judges it. Any exception on the
way (a missing executable, a failed git read, a judge that crashed) yields
:class:`Unknown`, which never passes. A core gate refuses on it, unlike a hook's
``CANNOT_EVALUATE`` that fails open to avoid lockout (BLUEPRINT §17.6.5).

The per-read seams (#3509) live here too. :func:`guarded_read` is for a caller with a
documented safe neutral that deliberately proceeds on it: the failure is logged and
carried, never silent. :func:`read_or_refuse` is for a read with no safe neutral, where
guessing is worse than refusing.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class EvidenceUnavailableError(RuntimeError):
    """A gate's evidence could not be read, so no verdict exists."""


@dataclass(frozen=True, slots=True)
class Pass:
    """The evidence was read and satisfies the gate."""


@dataclass(frozen=True, slots=True)
class Refuse:
    reason: str


@dataclass(frozen=True, slots=True)
class Unknown:
    cause: str


type Verdict = Pass | Refuse | Unknown


@dataclass(frozen=True, slots=True)
class GateResult[E]:
    gate_id: str
    verdict: Verdict
    remedy: str = ""
    evidence: E | None = None

    @property
    def passed(self) -> bool:
        return isinstance(self.verdict, Pass)

    def render(self) -> str:
        if isinstance(self.verdict, Pass):
            return f"[gate:{self.gate_id}] PASSED"
        if isinstance(self.verdict, Refuse):
            outcome = f"REFUSED: {self.verdict.reason}"
        else:
            outcome = f"DID NOT RUN: {self.verdict.cause.rstrip('.')}. No verdict recorded."
        remedy = f" Remedy: {self.remedy}" if self.remedy else ""
        return f"[gate:{self.gate_id}] {outcome}{remedy}"


def evaluate_gate[E](
    gate_id: str,
    *,
    collect: Callable[[], E],
    judge: Callable[[E], Verdict],
    remedy: str = "",
) -> GateResult[E]:
    """Collect *gate_id*'s evidence, then judge it; a failure in either step is :class:`Unknown`."""
    try:
        evidence = collect()
    except Exception as exc:
        logger.warning("gate %s: evidence could not be collected", gate_id, exc_info=True)
        return GateResult(gate_id, Unknown(_describe(exc)), remedy)
    try:
        verdict = judge(evidence)
    except Exception as exc:
        logger.warning("gate %s: the evidence could not be judged", gate_id, exc_info=True)
        return GateResult(gate_id, Unknown(_describe(exc)), remedy, evidence)
    return GateResult(gate_id, verdict, remedy, evidence)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True, slots=True)
class ReadOutcome[T]:
    """One read's result, with the failure kept distinct from a genuine empty."""

    value: T
    failed: bool
    error: Exception | None = None


def guarded_read[T](what: str, read: Callable[[], T], *, neutral: T) -> ReadOutcome[T]:
    """Run *read*, degrading to *neutral* on failure — loudly, and distinguishably.

    *what* is a short human phrase naming the read ("mr author", "file diff"); it is
    what the operator greps for after the fact.
    """
    try:
        return ReadOutcome(value=read(), failed=False)
    except Exception as exc:
        logger.warning("could not read %s (%s) — degrading to the neutral value", what, exc, exc_info=True)
        return ReadOutcome(value=neutral, failed=True, error=exc)


def read_or_refuse[T](what: str, read: Callable[[], T]) -> T:
    """Run *read*, raising :class:`EvidenceUnavailableError` on failure.

    For reads whose neutral value would be a GUESS with outbound consequences.
    """
    try:
        return read()
    except Exception as exc:
        msg = f"could not resolve {what} ({exc}) — refusing rather than guessing"
        raise EvidenceUnavailableError(msg) from exc
