"""Dispatch a resolved ``t3 eval run`` to its lane (matrix / pass@k / single-trial).

Held apart from :mod:`teatree.cli.eval.app` (which is at its module-LOC cap) so
the command body stays the typer surface + argument resolution while this owns the
final fan-out: once the specs and every flag are resolved, pick the right lane —
the ``--models`` comparison matrix, the ``--trials k`` pass@k sweep, or the
single-trial path (the only one that carries the ``--escalate-on-fail`` config).
The flag bundle is a frozen dataclass so the three lane calls read one resolved
value set rather than ~15 positional kwargs threaded through the command body.
"""

import dataclasses
from pathlib import Path

from claude_agent_sdk.types import EffortLevel

from teatree.cli.eval.escalate import EscalationConfig
from teatree.cli.eval.multi_trial import run_model_matrix_lane, run_pass_at_k_lane
from teatree.cli.eval.run_modes import DEFAULT_COST_REGRESSION_TOLERANCE
from teatree.cli.eval.single_trial import SingleTrialGates, run_single_trial
from teatree.eval.models import EvalSpec
from teatree.eval.report import JudgeGrader


@dataclasses.dataclass(frozen=True)
class ResolvedRun:
    """Every resolved ``t3 eval run`` value the lane dispatch consumes.

    Built once in the command body from the validated CLI arguments, then handed to
    :func:`dispatch_resolved_run`, which selects the lane. Keeping the bundle frozen
    means the three lane calls share one immutable resolved set.
    """

    backend: str
    max_turns: int | None
    transcript_dir: Path | None
    require_executed: bool
    max_budget_usd: float
    effort: EffortLevel
    parallel: int
    output_format: str
    judge: bool
    transcript_html: Path | None
    summary_md: Path | None
    trials: int
    require: str
    models: str | None
    persist: bool
    baseline: bool
    gate_regressions: bool
    gate_cost_regression: bool
    cost_regression_tolerance: float = DEFAULT_COST_REGRESSION_TOLERANCE
    gate_cost_bounds: bool = False


def dispatch_resolved_run(
    specs: list[EvalSpec],
    run: ResolvedRun,
    *,
    grader: JudgeGrader | None,
    escalation: EscalationConfig | None,
) -> None:
    """Fan the resolved run out to its lane.

    ``--models`` → the model-comparison matrix; ``--trials k>1`` → the pass@k
    sweep; otherwise the single-trial path (the only lane that carries the
    ``--escalate-on-fail`` config — the matrix/pass@k lanes already aggregate
    across trials).
    """
    if run.models is not None:
        run_model_matrix_lane(
            specs,
            models=run.models,
            max_turns=run.max_turns,
            trials=run.trials,
            require=run.require,
            output_format=run.output_format,
            persist=run.persist,
            baseline=run.baseline,
            gate_regressions=run.gate_regressions,
            gate_cost_regression=run.gate_cost_regression,
            cost_regression_tolerance=run.cost_regression_tolerance,
            gate_cost_bounds=run.gate_cost_bounds,
            grader=grader,
            require_executed=run.require_executed,
            max_budget_usd=run.max_budget_usd,
            effort=run.effort,
        )
        return
    if run.trials > 1:
        run_pass_at_k_lane(
            specs,
            max_turns=run.max_turns,
            trials=run.trials,
            require=run.require,
            output_format=run.output_format,
            persist=run.persist,
            baseline=run.baseline,
            gate_regressions=run.gate_regressions,
            gate_cost_regression=run.gate_cost_regression,
            cost_regression_tolerance=run.cost_regression_tolerance,
            gate_cost_bounds=run.gate_cost_bounds,
            grader=grader,
            require_executed=run.require_executed,
            max_budget_usd=run.max_budget_usd,
            effort=run.effort,
            transcript_html=run.transcript_html,
            summary_md=run.summary_md,
        )
        return
    run_single_trial(
        specs,
        backend=run.backend,
        max_turns=run.max_turns,
        transcript_dir=run.transcript_dir,
        require_executed=run.require_executed,
        max_budget_usd=run.max_budget_usd,
        effort=run.effort,
        parallel=run.parallel,
        output_format=run.output_format,
        grader=grader,
        judge=run.judge,
        transcript_html=run.transcript_html,
        summary_md=run.summary_md,
        gates=SingleTrialGates(
            persist=run.persist,
            baseline=run.baseline,
            gate_regressions=run.gate_regressions,
            gate_cost_regression=run.gate_cost_regression,
            cost_regression_tolerance=run.cost_regression_tolerance,
            gate_cost_bounds=run.gate_cost_bounds,
        ),
        escalation=escalation,
    )
