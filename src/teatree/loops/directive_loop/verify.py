"""The VERIFY phase — gather the five evidence classes, then decide (north-star PR-7).

:func:`gather_evidence` reads the design's five evidence classes for a directive —
activation live, acceptance green, behavior probe clean, no collateral regression,
zero open critic findings — each behind an injectable seam so the whole pipeline is
table-tested without a real clock, a real pytest run, or a live critic.
:func:`verify_and_decide` applies the pure :func:`decide_fulfilment` rule and drives
the directive to ``FULFILLED`` or ``REVERT_PENDING``. On a revert, the overlay config
is rolled back INSTANTLY (:func:`rollback_and_request_revert`) — the human then only
ratifies the code revert.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.models import CriticFinding, Directive
from teatree.loops.directive_loop.configure import clear_activation
from teatree.loops.directive_loop.decide import FulfilDecision, VerifyEvidence, decide_fulfilment
from teatree.loops.directive_loop.probes import resolve_probe
from teatree.loops.outer_loop.score import read_score
from teatree.loops.shared.regression import no_collateral_regression
from teatree.loops.shared.score_snapshot import snapshot_to_score
from teatree.utils.acceptance_runner import run_acceptance_tests

_MISSING = object()


@dataclass(frozen=True, slots=True)
class VerifySeams:
    """Injectable readers for the five evidence classes — all-default in production.

    Each ``None`` seam resolves to its real reader; tests supply fakes to drive each
    evidence class independently (the anti-vacuity posture — every class proven to
    fail the decision on its own).
    """

    activation_reader: Callable[[Directive], bool] | None = None
    acceptance_reader: Callable[[Directive], bool] | None = None
    probe_reader: Callable[[Directive, datetime], str] | None = None
    regression_reader: Callable[[Directive], str] | None = None
    critic_findings_reader: Callable[[Directive], int] | None = None


def horizon_elapsed(directive: Directive, *, verify_days: int, now: datetime) -> bool:
    """Whether the verify horizon has elapsed since the activation clock was armed."""
    started = directive.verify_started_at
    if started is None:
        return False
    return now >= started + timedelta(days=verify_days)


def gather_evidence(
    directive: Directive, *, now: datetime | None = None, seams: VerifySeams | None = None
) -> VerifyEvidence:
    """Read all five evidence classes for *directive* (via seams or the real readers)."""
    resolved = seams or VerifySeams()
    moment = now or timezone.now()
    return VerifyEvidence(
        activation_live=(resolved.activation_reader or _activation_live)(directive),
        acceptance_green=(resolved.acceptance_reader or _acceptance_green)(directive),
        probe_finding=(resolved.probe_reader or _probe_finding)(directive, moment),
        collateral_regression=(resolved.regression_reader or _collateral_regression)(directive),
        open_critic_findings=(resolved.critic_findings_reader or _open_critic_findings)(directive),
    )


def verify_and_decide(
    directive: Directive, *, now: datetime | None = None, seams: VerifySeams | None = None
) -> FulfilDecision:
    """Gather evidence, decide, and resolve the directive to FULFILLED or REVERT_PENDING."""
    decision = decide_fulfilment(gather_evidence(directive, now=now, seams=seams))
    if decision.fulfilled:
        directive.record_fulfilled(reason=decision.reason)
    else:
        rollback_and_request_revert(directive, reason=decision.reason)
    return decision


def rollback_and_request_revert(directive: Directive, *, reason: str) -> None:
    """Instant config rollback + ``VERIFYING`` → ``REVERT_PENDING`` (human ratifies the CODE revert)."""
    clear_activation(directive)
    directive.request_revert(reason=reason)


def _activation_live(directive: Directive) -> bool:
    """Evidence 1: the ratified activation reads back through the REAL resolver."""
    sketch = directive.sketch
    if sketch is None:
        return False
    effective = getattr(get_effective_settings(sketch.activation_scope or None), sketch.setting_key, _MISSING)
    return effective == sketch.activation_value


def _acceptance_green(directive: Directive) -> bool:
    """Evidence 2: the sketch's acceptance-test node ids pass at the merged tree.

    An ``activation_only`` directive names no acceptance tests (the mechanism was
    proven by its own PR's CI) — nothing to run here, so evidence 1/3/4/5 carry it.
    """
    sketch = directive.sketch
    if sketch is None:
        return False
    node_ids = list(sketch.acceptance_tests)
    if not node_ids:
        return True
    return run_acceptance_tests(node_ids)


def _probe_finding(directive: Directive, now: datetime) -> str:
    """Evidence 3: the sketch's behavior probe over ``[activation_applied_at, now]``."""
    sketch = directive.sketch
    if sketch is None or not sketch.behavior_probe:
        return ""
    probe = resolve_probe(sketch.behavior_probe)
    if probe is None:
        return f"behavior_probe {sketch.behavior_probe!r} is not in the probe catalog"
    since = directive.activation_applied_at or now
    return probe(sketch.activation_scope, since) or ""


def _collateral_regression(directive: Directive) -> str:
    """Evidence 4: no non-directive factory signal turned worse vs the admission baseline."""
    baseline_snapshot = directive.baseline_snapshot
    if baseline_snapshot is None:
        return "no admission baseline — cannot prove no collateral regression"
    baseline = snapshot_to_score(baseline_snapshot)
    post = read_score(overlay=directive.scope_overlay)
    return no_collateral_regression(baseline, post) or ""


def _open_critic_findings(directive: Directive) -> int:
    """Evidence 5: count of open critic findings on the mechanism ticket (all transitions)."""
    ticket = directive.ticket
    if ticket is None:
        return 0
    return CriticFinding.objects.filter(ticket=ticket).count()
