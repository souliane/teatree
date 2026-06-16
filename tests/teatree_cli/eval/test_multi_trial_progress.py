"""Per-scenario progress streaming in the metered pass@k lane.

The metered suite runs each scenario inside a list-comprehension; with no
per-scenario emission a hang produces ZERO output until the whole suite ends
(and GitHub never exposes an in-progress job's log blob), making a hung run
indistinguishable from a slow one. ``run_pass_at_k_lane`` must therefore emit a
flushed ``RUN``/``DONE`` line per scenario so the CI log streams live progress
and a stall is pinpointed to the last ``RUN <scenario>`` line. These tests pin
that contract with a fake runner — never the real SDK.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
    )


def _clean_run(spec: EvalSpec) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.01,
    )


class _CleanRunner:
    def run(self, spec: EvalSpec) -> EvalRun:
        return _clean_run(spec)


@pytest.fixture(autouse=True)
def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lane builds the real SDK runner internally; swap it for a deterministic
    # clean runner so no network/metered call is made.
    monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _CleanRunner())


class TestPerScenarioProgressStreaming:
    def test_emits_a_run_and_done_line_per_scenario(self, capsys: pytest.CaptureFixture[str]) -> None:
        specs = [_spec("alpha"), _spec("beta"), _spec("gamma")]
        run_pass_at_k_lane(specs, max_turns=None, trials=1, require="any", output_format="text")
        err = capsys.readouterr().err
        # Every scenario gets a bracketed, indexed RUN line BEFORE it runs and a
        # DONE line after — so a hang leaves the last RUN line as the pinpoint.
        for index, name in enumerate(["alpha", "beta", "gamma"], start=1):
            assert f"RUN  [{index}/3] {name}" in err
            assert f"DONE [{index}/3] {name}" in err

    def test_run_line_precedes_done_line_for_the_same_scenario(self, capsys: pytest.CaptureFixture[str]) -> None:
        run_pass_at_k_lane([_spec("alpha")], max_turns=None, trials=1, require="any", output_format="text")
        err = capsys.readouterr().err
        assert err.index("RUN  [1/1] alpha") < err.index("DONE [1/1] alpha")
