"""The shipped catalog can never reach the frontier (Opus) tier by ANY path.

souliane/teatree run 28515055436 (the CI workflow's failing weekly dispatch)
surfaced that a scenario declaring ``phase: coding``/``reviewing``/``planning``
silently resolves through ``DEFAULT_PHASE_MODELS`` to the ``frontier`` tier —
contradicting the "the automated eval lane defaults to Sonnet 5" goal even
though no scenario declares ``tier: frontier`` directly. This pins the fix at
the CATALOG level: not "no scenario says frontier" but "no scenario CAN reach
frontier by any resolution path" (``model`` / ``tier`` / ``phase`` / default).
"""

from teatree.agents.model_tiering import TIER_MODELS
from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import load_eval_yaml
from teatree.eval.model_resolution import resolve_eval_model


def test_no_shipped_scenario_resolves_to_the_frontier_tier() -> None:
    frontier_model = TIER_MODELS["frontier"]
    offenders = [
        f"{spec.name} ({spec.source_path.name}, phase={spec.phase!r} tier={spec.tier!r})"
        for path in sorted(SCENARIOS_DIR.glob("*.yaml"))
        for spec in load_eval_yaml(path)
        if resolve_eval_model(spec) == frontier_model
    ]
    assert not offenders, (
        "these scenarios silently resolve to the frontier (Opus) tier via "
        "phase/tier — pin them to `tier: balanced` (or an explicit model:) so "
        "the metered CI lane never accidentally rides Opus:\n  " + "\n  ".join(offenders)
    )
