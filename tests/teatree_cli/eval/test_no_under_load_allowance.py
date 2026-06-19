"""The metered lane has NO known-red allowance — an under_load failure reds it.

The shrink-only ``under_load`` ratchet (``evals/under_load_known_red.yaml`` +
``teatree.eval.under_load_ratchet`` + the ``--gate-under-load-ratchet`` flag) has
been removed. Every eval lane must be GREEN, and a red scenario fails the run
outright — there is no metered allowance, no baseline, no shrink-only tolerance.

These tests pin that:

*   a failing ``under_load``-lane scenario makes :func:`run_pass_at_k_lane`
    return ``True`` (the run is red) — exactly like a failing ``clean_room``
    scenario, with no exemption; and
*   the removed knobs are gone for good (the CLI flag is rejected, the gate class
    and the ratchet module no longer import) so the allowance cannot be
    reintroduced silently.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.models import UNDER_LOAD_LANE, EvalRun, EvalSpec, Matcher


def _under_load_spec(name: str) -> EvalSpec:
    """A behavioral ``under_load``-lane spec whose positive matcher cannot be met."""
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/rules/SKILL.md",
        prompt="do",
        # A positive matcher the empty-tool-call runner below can never satisfy → FAIL.
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="never-emitted"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        lane=UNDER_LOAD_LANE,
        judge=None,
    )


class _NoToolCallRunner:
    """A runner that makes no tool call, so a positive-matcher scenario fails."""

    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=("thinking, no tool call",),
            terminal_reason="end_turn",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


class TestUnderLoadFailureRedsTheLane:
    def test_failing_under_load_scenario_makes_the_lane_red(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The core mandate: an under_load behavioural-drift failure is a real
        # failure. With the ratchet gone there is no baseline to absorb it, so the
        # lane is red — the same verdict a clean_room failure produces.
        monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _NoToolCallRunner())
        failed = run_pass_at_k_lane(
            [_under_load_spec("drifting_under_load")],
            max_turns=None,
            trials=2,
            require="any",
            output_format="text",
            persist=False,
            model_override="claude-sonnet-4-6",  # suppress sys.exit so we can read the return value
        )
        assert failed is True, (
            "a failing under_load scenario must red the lane — there is no known-red "
            "allowance and no shrink-only ratchet to exempt it"
        )


class TestRemovedRatchetKnobsAreGone:
    def test_gate_under_load_ratchet_flag_is_rejected(self) -> None:
        # The flag is removed; passing it must be a usage error, never a silent no-op.
        from teatree.cli import app  # noqa: PLC0415

        result = CliRunner().invoke(app, ["eval", "run", "--gate-under-load-ratchet", "--no-persist"])
        assert result.exit_code != 0
        assert "--gate-under-load-ratchet" in result.output or "No such option" in result.output

    def test_ratchet_module_no_longer_imports(self) -> None:
        # The shrink-only ratchet module is deleted, so importing it must fail.
        with pytest.raises(ModuleNotFoundError):
            __import__("teatree.eval.under_load_ratchet")

    def test_ratchet_gate_class_no_longer_exists(self) -> None:
        from teatree.cli.eval import run_modes  # noqa: PLC0415

        assert not hasattr(run_modes, "UnderLoadRatchetGate")
