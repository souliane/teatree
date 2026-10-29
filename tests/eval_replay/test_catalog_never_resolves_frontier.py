"""The shipped catalog can never reach the frontier (Opus) tier by ANY path.

souliane/teatree run 28515055436 (the CI workflow's failing weekly dispatch)
surfaced that a scenario declaring ``phase: coding``/``reviewing``/``planning``
silently resolves through ``phase_models()`` to the ``frontier`` tier —
contradicting the "the automated eval lane defaults to the balanced tier" goal even
though no scenario declares ``tier: frontier`` directly. This pins the fix at
the CATALOG level: not "no scenario says frontier" but "no scenario CAN reach
frontier by any resolution path" (``model`` / ``tier`` / ``phase`` / default).
"""

from typing import cast
from unittest.mock import patch

from teatree.agents.model_tiering import tier_models
from teatree.config.registries import COLD_SETTINGS
from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import load_eval_yaml
from teatree.eval.model_resolution import resolve_eval_model


def test_no_shipped_scenario_resolves_to_the_frontier_tier() -> None:
    defaults = cast("dict[str, str]", COLD_SETTINGS["agent_tier_models"].default)
    with patch.dict(defaults, {"frontier": "claude-opus-5-5"}):
        frontier_model = tier_models()["frontier"]
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
