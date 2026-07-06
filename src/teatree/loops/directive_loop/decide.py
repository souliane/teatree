"""The pure fulfil rule — all five evidence classes green, or REVERT (north-star PR-7).

A directive is FULFILLED only when EVERY evidence class the design defines is green;
anything less parks it in ``REVERT_PENDING``. The rule is a pure fold over a
:class:`VerifyEvidence` value (the readers live in :mod:`verify`), so the
keep-or-revert judgment is deterministic and table-tested — each class proven to
fail the decision independently.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerifyEvidence:
    """The five evidence classes read at the verify horizon.

    ``probe_finding`` / ``collateral_regression`` are finding strings — empty means
    clean. ``open_critic_findings`` is the count of unresolved critic findings on the
    mechanism ticket (all transitions).
    """

    activation_live: bool
    acceptance_green: bool
    probe_finding: str
    collateral_regression: str
    open_critic_findings: int


@dataclass(frozen=True, slots=True)
class FulfilDecision:
    """The keep-or-revert outcome plus the load-bearing reason."""

    fulfilled: bool
    reason: str


def decide_fulfilment(evidence: VerifyEvidence) -> FulfilDecision:
    """FULFILLED iff all five evidence classes pass; else REVERT with the first failure.

    Checked in the design's order — activation readable, acceptance green, behavior
    probe clean, no collateral regression, zero open critic findings. The first class
    that fails names the revert reason; a merely-green auto-change that is not clean
    (an open critic finding) is NOT fulfilled.
    """
    if not evidence.activation_live:
        return FulfilDecision(fulfilled=False, reason="activation not readable back through get_effective_settings")
    if not evidence.acceptance_green:
        return FulfilDecision(fulfilled=False, reason="acceptance tests not green at the merged tree")
    if evidence.probe_finding:
        return FulfilDecision(fulfilled=False, reason=f"behavior probe found a violation: {evidence.probe_finding}")
    if evidence.collateral_regression:
        return FulfilDecision(fulfilled=False, reason=f"collateral regression: {evidence.collateral_regression}")
    if evidence.open_critic_findings:
        return FulfilDecision(
            fulfilled=False, reason=f"{evidence.open_critic_findings} open CriticFinding row(s) at the delivered head"
        )
    return FulfilDecision(fulfilled=True, reason="all five evidence classes green")
