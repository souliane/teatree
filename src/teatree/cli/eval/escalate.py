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
    lane goes RED;
*   every escalation trial SKIPS → ``unresolved`` — the re-run never happened, so
    nothing disambiguated trial 1; the failure stands and the lane goes RED.

Only ``flaky`` clears. "We could not re-run it" is not evidence the agent is
capable, so an unresolved escalation must never green a lane on an unproven pass.

So the lane is cheap on the common all-green path (no escalation runs at all) and
only spends extra trials to disambiguate a failure into flaky-vs-real before it
reds CI. A scenario that passed or skipped the single trial is never re-run.

The runner is injected (any callable mapping ``EvalSpec -> ScenarioResult``), so
tests drive deterministic stubs and production passes the same metered closure the
single-trial path builds.
"""

import dataclasses
from collections import Counter
from collections.abc import Callable
from typing import Literal

from teatree.eval.harness_failure import measured_nothing
from teatree.eval.models import CAP_TERMINAL_REASONS, EvalSpec
from teatree.eval.pass_at_k import PassAtKResult, run_pass_at_k
from teatree.eval.report import ScenarioResult
from teatree.eval.surface import is_advisory

#: An injected trial runner — maps a spec to one graded :class:`ScenarioResult`.
TrialRunner = Callable[[EvalSpec], ScenarioResult]

EscalationClass = Literal["flaky", "confirmed", "unresolved"]

#: The ONLY classification that clears a trial-1 failure; every other one reds the lane.
CLEARING_CLASS: EscalationClass = "flaky"


@dataclasses.dataclass(frozen=True)
class EscalationConfig:
    """Adaptive escalate-on-fail knobs (the ``--escalate-on-fail`` PR-lane path).

    When set, a single-trial FAILURE is not yet a hard red: each failed scenario
    is re-run at ``escalate_trials`` higher trials. Only a scenario that passes on
    some escalation trial is cleared (``flaky``, not red); one whose trials all fail
    (``confirmed``) or all skip (``unresolved``) reds the lane. Lives here
    (the escalation module) so both the single-trial runner and the CLI validator
    import it without a cross-module import cycle.
    """

    escalate_trials: int


@dataclasses.dataclass(frozen=True)
class EscalationOutcome:
    """One re-run scenario's escalation verdict.

    ``classification`` is ``flaky`` when the scenario passed at least one of its
    ``trials`` escalation trials (capable agent, trial-1 noise), ``confirmed`` when
    every escalation trial failed (a real, non-flaky failure), or ``unresolved`` when
    every escalation trial skipped (the re-run never happened).

    ``advisory`` carries the scenario's question SURFACE through to the verdict: an
    ``interactive``-surface scenario is re-run and reported exactly like any other,
    but never reds the lane (#3855).

    ``cap_tainted`` records that at least one escalation trial was cap-truncated
    (``max_turns``/budget/timeout), so the trial evidence is incomplete. It is
    REPORTED by both renderers and never gates: the cap veto belongs to the weekly
    gate (:attr:`~teatree.eval.pass_at_k.PassAtKResult.ok`), and applying it here
    reds a scenario that passed a majority of its escalation trials (#4243).
    """

    spec_name: str
    trials: int
    passes: int
    classification: EscalationClass
    advisory: bool = False
    cap_tainted: bool = False

    @property
    def is_hard_red(self) -> bool:
        """Anything but a ``flaky`` clear reds the lane, unless the scenario is advisory.

        Phrased as "did not clear" rather than "is confirmed" so the DEFAULT is to
        gate: only a demonstrated pass retires the trial-1 failure, and a future
        classification cannot silently green a lane by not being ``confirmed``.

        An ``advisory`` (``surface: interactive``) scenario never reds, however
        solidly confirmed: its verdict rides a bundled claude CLI's ``AskUserQuestion``
        rendering rather than the question contract teatree owns (#3855).
        """
        return self.classification != CLEARING_CLASS and not self.advisory


@dataclasses.dataclass(frozen=True)
class EscalationReport:
    """The aggregate escalation result: the per-scenario outcomes + the lane verdict.

    ``hard_red`` is ``True`` iff at least one GATING escalated scenario failed to
    clear — ``confirmed`` (every trial failed) or ``unresolved`` (every trial
    skipped) — the signal the CLI uses to exit non-zero.
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
    # Counted by CLASSIFICATION, not by ``is_hard_red``: an advisory scenario that
    # failed every escalation trial IS confirmed and must read as such — it simply
    # does not gate. Counting it as flaky would hide a real interactive regression.
    # Every class is counted on its own for the same reason — folding ``unresolved``
    # into a flaky remainder would report a never-re-run failure as a cleared one.
    counts = Counter(outcome.classification for outcome in report.outcomes)
    header = (
        f"**Escalation** — {counts['confirmed']} confirmed, {counts['flaky']} flaky, "
        f"{counts['unresolved']} unresolved (of {len(report.outcomes)} re-run)"
    )
    table = [
        "| scenario | escalation trials | classification |",
        "| --- | --- | --- |",
        *(
            f"| {outcome.spec_name} | {outcome.passes}/{outcome.trials} | {describe_classification(outcome)} |"
            for outcome in report.outcomes
        ),
    ]
    return "\n".join([header, "", *table, ""])


def describe_classification(outcome: EscalationOutcome) -> str:
    """The outcome's classification, tagged with every reported-but-non-gating qualifier."""
    tags = [tag for tag, present in (("advisory", outcome.advisory), ("cap-truncated", outcome.cap_tainted)) if present]
    return f"{outcome.classification} ({', '.join(tags)})" if tags else outcome.classification


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
    escalation trial is enough — and the only thing enough — to clear it as ``flaky``.
    An escalation whose every trial SKIPPED disambiguated nothing, so it classifies as
    ``unresolved`` and still reds the lane rather than clearing on an absent verdict.
    """
    if escalate_trials < 2:  # noqa: PLR2004 — one trial is no escalation; the trial-1 result already covers it.
        msg = f"escalate_trials must be >= 2 (got {escalate_trials}); a single trial is not an escalation."
        raise ValueError(msg)
    outcomes: list[EscalationOutcome] = []
    for result in initial:
        if not _failed(result):
            continue
        aggregate = run_pass_at_k(result.spec, runner, k=escalate_trials, require="any")
        classification = _classify(aggregate)
        outcomes.append(
            EscalationOutcome(
                spec_name=result.spec.name,
                trials=aggregate.trials,
                passes=aggregate.passes,
                classification=classification,
                # A confirmed harness failure measured nothing, so there is no verdict
                # for the surface to excuse — it stays hard red on any surface (#3922).
                advisory=is_advisory(result.spec) and not measured_nothing(result.run.terminal_reason),
                cap_tainted=aggregate.terminal_reason in CAP_TERMINAL_REASONS,
            )
        )
    return EscalationReport(outcomes=outcomes)


def _classify(aggregate: PassAtKResult) -> EscalationClass:
    """The escalation verdict for one re-run scenario's aggregate.

    ``PassAtKResult.ok`` is ``True`` for an all-skipped aggregate, which is right for
    a plain pass@k run (a skip is not a failure) and wrong as a CLEARING rule here:
    escalation exists to disambiguate a real failure, and a re-run that never happened
    disambiguates nothing. So an all-skipped aggregate is ``unresolved`` — reported as
    what it is, and still red — rather than reading as a proven-capable ``flaky``.

    The non-skipped case keys on ``passes``, NOT on ``aggregate.ok``: ``ok`` is the
    weekly gate's verdict and vetoes on cap taint (#2192), which would red a scenario a
    majority of whose escalation trials passed cleanly (#4243). One clean pass clears.
    """
    if aggregate.skipped:
        return "unresolved"
    return CLEARING_CLASS if aggregate.passes >= 1 else "confirmed"
