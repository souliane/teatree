"""The MEASURE + DECIDE phases — post-horizon score and keep-only-if-better (T4-PR-3).

After an experiment's fix merges the loop arms a measurement horizon
(:func:`arm_measurement`); once :func:`horizon_elapsed` days pass it takes a post
:func:`~teatree.loops.outer_loop.score.read_score` and applies the pure
:func:`~teatree.loops.outer_loop.decide.decide_keep` rule
(:func:`measure_and_decide`). A non-improving experiment is never kept — it moves
to ``REVERT_PENDING`` for a human-ratified revert.

MEASURE is a time+merge-count window, NOT causal attribution — a horizon-window
delta is confounded by unrelated merges. The no-regression-anywhere rule and the
human-ratified revert bound the risk; a KEPT decision is "correlated better", not
"proven caused". This is the known weakest link (documented in BLUEPRINT).
"""

from datetime import datetime, timedelta

from teatree.core.factory_score import FactoryScore, ScoredSignal
from teatree.core.models import FactoryScoreSnapshot, OuterLoopExperiment
from teatree.loops.outer_loop.decide import Decision, decide_keep
from teatree.loops.outer_loop.score import read_score
from teatree.utils.git_branch import head_sha


def arm_measurement(experiment: OuterLoopExperiment, *, now: datetime | None = None) -> None:
    """``IMPLEMENTING`` → ``MEASURING``: start the post-merge horizon clock."""
    experiment.arm_measure(now=now)


def horizon_elapsed(experiment: OuterLoopExperiment, *, measure_days: int, now: datetime) -> bool:
    """Whether the measurement horizon has elapsed since the clock was armed."""
    started = experiment.measure_started_at
    if started is None:
        return False
    return now >= started + timedelta(days=measure_days)


def measure_and_decide(
    experiment: OuterLoopExperiment,
    *,
    overlay: str = "",
    now: datetime | None = None,
    post_score: FactoryScore | None = None,
) -> Decision:
    """Take the post score, apply the keep-rule, and resolve the experiment.

    KEEP → ``KEPT`` bound to the current HEAD sha; otherwise → ``REVERT_PENDING``.
    The baseline is the experiment's admission snapshot; a missing baseline is a
    conservative REVERT (we cannot prove improvement without it).
    """
    resolved_post = post_score if post_score is not None else read_score(overlay=overlay, now=now)
    post_snapshot = FactoryScoreSnapshot.objects.record_snapshot(
        resolved_post, tree_sha=_safe_head_sha(), overlay=overlay
    )
    baseline = _baseline_score(experiment)
    if baseline is None:
        experiment.request_revert(
            post_snapshot=post_snapshot, reason="no admission baseline — cannot prove improvement"
        )
        return Decision(keep=False, reason="no admission baseline")
    decision = decide_keep(
        baseline=baseline,
        post=resolved_post,
        target_provider_id=experiment.target_provider_id,
        regress_band=experiment.regress_band,
    )
    if decision.keep:
        experiment.record_kept(post_snapshot=post_snapshot, merged_sha=post_snapshot.tree_sha, reason=decision.reason)
    else:
        experiment.request_revert(post_snapshot=post_snapshot, reason=decision.reason)
    return decision


def _baseline_score(experiment: OuterLoopExperiment) -> FactoryScore | None:
    """Reconstruct the admission FactoryScore from the experiment's baseline snapshot."""
    snapshot = experiment.baseline_snapshot
    if snapshot is None:
        return None
    return _snapshot_to_score(snapshot)


def _snapshot_to_score(snapshot: FactoryScoreSnapshot) -> FactoryScore:
    signals = [
        ScoredSignal(
            provider_id=raw["provider_id"],
            status=raw["status"],
            value=raw.get("value"),
            normalized=raw.get("normalized"),
            weight=raw.get("weight", 0.0),
            covered=raw.get("covered", False),
            red=raw.get("red", False),
            verdict=raw.get("verdict", ""),
        )
        for raw in snapshot.signals
    ]
    return FactoryScore(
        aggregate=snapshot.aggregate,
        verdict=snapshot.verdict,
        coverage=snapshot.coverage,
        coverage_floor=snapshot.coverage_floor,
        recipe_sha=snapshot.recipe_sha,
        recipe_approved=snapshot.recipe_approved,
        window_days=snapshot.window_days,
        signals=signals,
    )


def _safe_head_sha() -> str:
    try:
        return head_sha() or ""
    except Exception:  # noqa: BLE001 — provenance is best-effort, never fatal to a measure
        return ""
