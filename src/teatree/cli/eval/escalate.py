"""Adaptive escalate-on-fail for the cheap single-trial PR lane.

The selective-PR eval runs each changed scenario ONCE (``--trials 1``) for fast,
cheap feedback. A single LLM trial is noisy, so a lone red trial is not yet proof
of a real failure — it may be an unlucky sample of a flaky-but-capable agent.

``escalate_failures`` closes that gap WITHOUT paying for a full ``--trials k``
sweep on every scenario: it re-runs ONLY the scenarios that failed the single
trial, each at ``escalate_trials`` higher trials, and classifies the result:

*   it passes on ANY escalation trial → ``flaky`` — the agent IS capable of the
    right behavior, so trial 1 was noise; this is NOT a hard red;
*   every escalation trial fails → ``confirmed`` — a real, non-flaky failure; the
    lane goes RED.

So the lane is cheap on the common all-green path (no escalation runs at all) and
only spends extra trials to disambiguate a failure into flaky-vs-real before it
reds CI. A scenario that passed or skipped the single trial is never re-run.

The runner is injected (any callable mapping ``EvalSpec -> ScenarioResult``), so
tests drive deterministic stubs and production passes the same metered closure the
single-trial path builds.
"""

import dataclasses
from collections.abc import Callable
from typing import Literal

from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.report import ScenarioResult

#: An injected trial runner — maps a spec to one graded :class:`ScenarioResult`.
TrialRunner = Callable[[EvalSpec], ScenarioResult]

EscalationClass = Literal["flaky", "confirmed"]


@dataclasses.dataclass(frozen=True)
class EscalationConfig:
    """Adaptive escalate-on-fail knobs (the ``--escalate-on-fail`` PR-lane path).

    When set, a single-trial FAILURE is not yet a hard red: each failed scenario
    is re-run at ``escalate_trials`` higher trials. It reds the lane only if every
    escalation trial also fails (``confirmed``); a scenario that passes on any
    escalation trial is reported flaky-but-passing (``flaky``), not red. Lives here
    (the escalation module) so both the single-trial runner and the CLI validator
    import it without a cross-module import cycle.
    """

    escalate_trials: int


@dataclasses.dataclass(frozen=True)
class EscalationOutcome:
    """One re-run scenario's escalation verdict.

    ``classification`` is ``flaky`` when the scenario passed at least one of its
    ``trials`` escalation trials (capable agent, trial-1 noise) or ``confirmed``
    when every escalation trial failed (a real, non-flaky failure).
    """

    spec_name: str
    trials: int
    passes: int
    classification: EscalationClass

    @property
    def is_hard_red(self) -> bool:
        """Only a ``confirmed`` failure reds the lane; a ``flaky`` pass does not."""
        return self.classification == "confirmed"


@dataclasses.dataclass(frozen=True)
class EscalationReport:
    """The aggregate escalation result: the per-scenario outcomes + the lane verdict.

    ``hard_red`` is ``True`` iff at least one escalated scenario was ``confirmed``
    (every escalation trial failed) — the signal the CLI uses to exit non-zero.
    """

    outcomes: list[EscalationOutcome]

    @property
    def hard_red(self) -> bool:
        return any(outcome.is_hard_red for outcome in self.outcomes)


def render_escalation_markdown(report: EscalationReport) -> str:
    """A SANITIZED markdown section summarizing the escalation outcomes.

    Built ONLY from each outcome's name, trial counts, and classification — it
    never reads a transcript, so it is safe to append to the publish-safe
    ``--summary-md`` dashboard a PR's ``$GITHUB_STEP_SUMMARY`` renders. Empty
    report → an empty string (nothing was escalated).
    """
    if not report.outcomes:
        return ""
    confirmed = sum(1 for outcome in report.outcomes if outcome.is_hard_red)
    flaky = len(report.outcomes) - confirmed
    header = f"**Escalation** — {confirmed} confirmed, {flaky} flaky (of {len(report.outcomes)} re-run)"
    table = [
        "| scenario | escalation trials | classification |",
        "| --- | --- | --- |",
        *(
            f"| {outcome.spec_name} | {outcome.passes}/{outcome.trials} | {outcome.classification} |"
            for outcome in report.outcomes
        ),
    ]
    return "\n".join([header, "", *table, ""])


def _failed(result: ScenarioResult) -> bool:
    """A trial-1 result that is neither skipped nor passing — the escalation set."""
    return not result.skipped and not result.passed


def escalate_failures(
    initial: list[ScenarioResult],
    runner: TrialRunner,
    *,
    escalate_trials: int,
) -> EscalationReport:
    """Re-run only the trial-1 failures at ``escalate_trials`` and classify each.

    Returns the per-scenario :class:`EscalationOutcome` list (empty when nothing
    failed trial 1) and, via :attr:`EscalationReport.hard_red`, whether the lane
    must go RED. A scenario re-runs at ``require="any"`` semantics: passing on any
    escalation trial is enough to clear it as ``flaky``.
    """
    if escalate_trials < 2:  # noqa: PLR2004 — one trial is no escalation; the trial-1 result already covers it.
        msg = f"escalate_trials must be >= 2 (got {escalate_trials}); a single trial is not an escalation."
        raise ValueError(msg)
    outcomes: list[EscalationOutcome] = []
    for result in initial:
        if not _failed(result):
            continue
        aggregate = run_pass_at_k(result.spec, runner, k=escalate_trials, require="any")
        classification: EscalationClass = "flaky" if aggregate.ok else "confirmed"
        outcomes.append(
            EscalationOutcome(
                spec_name=result.spec.name,
                trials=aggregate.trials,
                passes=aggregate.passes,
                classification=classification,
            )
        )
    return EscalationReport(outcomes=outcomes)
