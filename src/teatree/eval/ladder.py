"""Escalation-ladder baseline generation — dispatch opus ONLY on sonnet's failures.

The cheapest-green baseline (``evals/presets/baseline.yaml``) records, per
scenario, the cheapest model tier that PASSES it. The full-matrix path
(``t3 eval benchmark`` / a 3-tier ``t3 eval run --models``) measures every model
on every scenario to derive that map — but a scenario haiku already passes never
needs sonnet or opus measured, so the full matrix over-pays for the baseline
question.

This module is the loss-free alternative. :func:`run_escalation_ladder` walks
each scenario up the tier ladder cheapest-first (cheap → balanced → frontier) and
STOPS at the first tier it passes: sonnet is dispatched only for the scenarios
haiku failed, and opus only for the scenarios that failed BOTH. The output is the
flat :class:`~teatree.eval.matrix.MatrixRow` list of the cells that actually ran —
fed through :func:`~teatree.eval.matrix.render_matrix_json` to the exact matrix
JSON ``t3 eval set-baseline`` already consumes, so the tier-derivation authority
is unchanged: a scenario is tiered to the cheapest model whose recorded cell
passed, and a scenario no tier passes gets no row that passed (set-baseline
surfaces it as a genuine failure, never silently tiers it to frontier).

CI SHAPE. Escalation is per-scenario and orthogonal to the shard-parallel CI
model: each metered shard runs its OWN subset of scenarios through the ladder
in-process (haiku → sonnet → opus for each of its scenarios), so the whole run
stays inside one account's usage window with no cross-pass orchestration and no
auto-rotation. The sharded fan-out still parallelises across scenarios exactly
like the full-matrix benchmark.
"""

import dataclasses

from teatree.agents.model_tiering import TIER_MODELS
from teatree.core.cost import tier_rank
from teatree.eval.matrix import MatrixRow
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import PassAtKResult, TrialRunner, run_pass_at_k


@dataclasses.dataclass(frozen=True)
class LadderPolicy:
    """How many trials each tier gets and what counts as a pass at that tier.

    Single-trial results are noisy, so the default is the noise-robust
    ``trials=1, require="all"`` — with ``trials=N`` a tier only counts as passed
    when EVERY trial passed, so the ladder escalates on any intermittent failure
    rather than tiering a flaky scenario to the cheaper model.
    """

    trials: int = 1
    require: str = "all"


#: The default noise-robust policy — a module-level singleton so it is not
#: constructed in a function signature default (ruff B008).
_DEFAULT_LADDER_POLICY = LadderPolicy()


def laddered_tier_models() -> list[str]:
    """The three tier model ids ordered cheapest-first (cheap < balanced < frontier)."""
    return sorted(TIER_MODELS.values(), key=_model_rank)


def _model_rank(model: str) -> int:
    """``tier_rank`` narrowed to a required ``str`` so the sorted element type stays ``str``."""
    return tier_rank(model)


def run_escalation_ladder(
    specs: list[EvalSpec],
    models: list[str],
    *,
    run_trial: TrialRunner,
    policy: LadderPolicy = _DEFAULT_LADDER_POLICY,
) -> list[MatrixRow]:
    """Escalate each spec cheapest-first, stopping at the first tier it passes.

    *models* is the tier model id list in cheapest-first order (see
    :func:`laddered_tier_models`). For each spec, a tier is dispatched ONLY when
    every cheaper tier FAILED the scenario — so a scenario haiku already passes
    never dispatches sonnet or opus. Escalation stops on a pass or a skip (a skip
    is "not provisioned", never a capability failure); only a graded FAIL climbs
    to the next tier. Returns the flat :class:`~teatree.eval.matrix.MatrixRow`
    list of the cells actually run — the never-reached tiers have no row, which
    :func:`~teatree.eval.matrix.render_matrix_json` renders as an absent cell.
    """
    rows: list[MatrixRow] = []
    for spec in specs:
        for model in models:
            result = run_pass_at_k(
                dataclasses.replace(spec, model=model), run_trial, k=policy.trials, require=policy.require
            )
            row = _row_from(result, model=model)
            rows.append(row)
            if not _is_graded_fail(row):
                break
    return rows


def resolve_ladder_tiers(rows: list[MatrixRow]) -> dict[str, str | None]:
    """Per scenario, the model id of its cheapest PASSING tier — or ``None`` if none passed.

    Relies on the escalation invariant: the ladder stops at the first pass, so the
    LAST recorded row for a scenario is its deciding cell — a pass names the
    cheapest passing tier; a fail (every tier failed) or a skip (not provisioned)
    yields ``None`` (no baseline tier, surfaced rather than tiered to frontier).
    This mirrors what ``t3 eval set-baseline`` derives from the emitted matrix
    JSON; it exists for the command's human-readable summary.
    """
    tiers: dict[str, str | None] = {}
    for row in rows:
        tiers[row.scenario] = row.model if row.passed else None
    return tiers


def _is_graded_fail(row: MatrixRow) -> bool:
    """A genuine capability FAIL — the only outcome that escalates to the next tier."""
    return not row.passed and not row.skipped


def _row_from(result: PassAtKResult, *, model: str) -> MatrixRow:
    """Fold one tier's pass@k result into a matrix cell stamped with its tier model id."""
    return MatrixRow(
        scenario=result.spec_name,
        model=model,
        passed=result.ok and not result.skipped,
        score=0.0 if result.skipped else result.pass_rate,
        trials=result.trials,
        skipped=result.skipped,
        cost_usd=result.cost_usd,
        usage=result.usage,
        terminal_reason=result.terminal_reason,
        main_cost_usd=result.main_cost_usd,
        aux_cost_usd=result.aux_cost_usd,
        main_usage=result.main_usage,
        aux_usage=result.aux_usage,
    )
