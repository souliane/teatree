"""The under_load ratchet wired into the pass@k lane (`run_pass_at_k_lane`).

Proves the END-TO-END gate, not just the pure ratchet function: with
``--gate-under-load-ratchet`` armed, a known-red under_load scenario failing
WITHIN the baseline lets the lane PASS (exit 0), while a NEW under_load failure
beyond the baseline — or a baselined scenario that now PASSES — fails the lane
(exit 1). The clean-room results are unaffected.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.under_load_ratchet import UnderLoadKnownRed


def _spec(name: str, *, lane: str = "clean_room") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        lane=lane,
    )


def _run(spec: EvalSpec, *, is_error: bool) -> EvalRun:
    # No matchers → a non-error run PASSES (all() over the empty matcher set is
    # True); an is_error run FAILS. That is all the gate's ok/not-ok needs.
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="error" if is_error else "end_turn",
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.01,
    )


class _Runner:
    """Deterministic runner: a scenario whose name starts ``red_`` errors (fails)."""

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec, is_error=spec.name.startswith("red_"))


@pytest.fixture(autouse=True)
def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _Runner())


def _patch_baseline(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    # The gate imports load_under_load_known_red locally; patch it at its source so
    # the test drives a synthetic baseline, not the checked-in file.
    monkeypatch.setattr(
        "teatree.eval.under_load_ratchet.load_under_load_known_red",
        lambda *a, **k: UnderLoadKnownRed(known_red=frozenset(names)),
    )


class TestRatchetGateInLane:
    def test_known_red_within_baseline_lets_the_lane_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_baseline(monkeypatch, {"red_known"})
        specs = [_spec("red_known", lane="under_load"), _spec("green_other", lane="under_load")]
        failed = run_pass_at_k_lane(
            specs,
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
            gate_under_load_ratchet=True,
            model_override="haiku",  # model_override suppresses sys.exit, returns the bool
        )
        # red_known is documented known-red; nothing else fails → the lane is green.
        assert failed is False

    def test_new_under_load_failure_beyond_baseline_reds_the_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ANTI-VACUITY: a fresh under_load red NOT in the baseline fails the lane.
        _patch_baseline(monkeypatch, {"red_known"})
        specs = [_spec("red_known", lane="under_load"), _spec("red_new", lane="under_load")]
        failed = run_pass_at_k_lane(
            specs,
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
            gate_under_load_ratchet=True,
            model_override="haiku",
        )
        assert failed is True

    def test_baselined_scenario_now_passing_reds_the_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ANTI-VACUITY (shrink-only): the baseline lists a scenario that now PASSES;
        # leaving it in must RED the lane until the file is shrunk.
        _patch_baseline(monkeypatch, {"red_known", "green_was_red"})
        specs = [_spec("red_known", lane="under_load"), _spec("green_was_red", lane="under_load")]
        failed = run_pass_at_k_lane(
            specs,
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
            gate_under_load_ratchet=True,
            model_override="haiku",
        )
        assert failed is True

    def test_disarmed_gate_reds_every_under_load_failure_like_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With the gate OFF, the lane's pre-existing behaviour stands: any failing
        # under_load scenario reds the lane (the ratchet only loosens when armed).
        _patch_baseline(monkeypatch, {"red_known"})
        specs = [_spec("red_known", lane="under_load")]
        failed = run_pass_at_k_lane(
            specs,
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
            gate_under_load_ratchet=False,
            model_override="haiku",
        )
        assert failed is True

    def test_clean_room_failure_still_reds_the_lane_with_the_gate_armed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The ratchet only governs under_load scenarios; a clean_room failure is
        # never excused by the baseline.
        _patch_baseline(monkeypatch, {"red_known"})
        specs = [_spec("red_clean", lane="clean_room")]
        failed = run_pass_at_k_lane(
            specs,
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
            gate_under_load_ratchet=True,
            model_override="haiku",
        )
        assert failed is True
