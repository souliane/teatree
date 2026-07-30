"""``t3 eval ladder`` — cheapest-green baseline generation via a tier escalation ladder.

Drives the real typer command with a FAKE api runner (a run passes at the tiers
listed for a scenario, fails elsewhere), so the reduced-dispatch matrix JSON and
the text summary are exercised end to end without a metered run. The docker
routing decision is asserted separately.
"""

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from teatree.agents.model_tiering import TIER_MODELS
from teatree.cli import app
from teatree.cli.eval.app import eval_app
from teatree.cli.eval.docker import DockerUnavailableError
from teatree.cli.eval.ladder import _docker_passthrough, _LadderFlags, ladder
from teatree.eval.models import EvalRun, EvalSpec

_HAIKU = TIER_MODELS["cheap"]
_SONNET = TIER_MODELS["balanced"]
_OPUS = TIER_MODELS["frontier"]


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name, scenario="sc", agent_path="skills/code/SKILL.md", prompt="p", matchers=(), source_path=Path("x.yaml")
    )


class _FakeRunner:
    """An api runner whose run PASSES only when ``spec.model`` is a scenario's listed tier."""

    def __init__(self, passing: dict[str, set[str]]) -> None:
        self._passing = passing

    def run(self, spec: EvalSpec) -> EvalRun:
        clean = spec.model in self._passing.get(spec.name, set())
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=("done",),
            terminal_reason="end_turn" if clean else "error",
            is_error=not clean,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


def _invoke(args: list[str], *, specs: list[EvalSpec], passing: dict[str, set[str]]) -> object:
    with (
        patch("teatree.cli.eval.ladder.should_route_to_docker", return_value=False),
        patch("teatree.cli.eval.ladder.discover_specs", return_value=specs),
        patch("teatree.cli.eval.ladder.make_runner", return_value=_FakeRunner(passing)),
    ):
        return CliRunner().invoke(app, ["eval", "ladder", *args])


class TestRegistration:
    def test_ladder_command_is_registered_on_the_eval_app(self) -> None:
        names = {command.name for command in eval_app.registered_commands}
        assert "ladder" in names
        assert callable(ladder)


class TestHelp:
    def test_help_renders(self) -> None:
        result = CliRunner().invoke(app, ["eval", "ladder", "--help"])
        assert result.exit_code == 0
        assert "escalation ladder" in result.output.lower()


class TestReducedDispatchJson:
    def test_haiku_pass_leaves_sonnet_and_opus_cells_absent(self) -> None:
        result = _invoke(["--format", "json"], specs=[_spec("alpha")], passing={"alpha": {_HAIKU}})
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        cells = payload["scenarios"][0]["results"]
        assert cells[_HAIKU]["passed"] is True
        # opus/sonnet never ran — an absent (null) cell, not a recorded FAIL.
        assert cells[_SONNET] is None
        assert cells[_OPUS] is None

    def test_sonnet_pass_records_haiku_fail_sonnet_pass_and_no_opus(self) -> None:
        result = _invoke(["--format", "json"], specs=[_spec("beta")], passing={"beta": {_SONNET}})
        assert result.exit_code == 0, result.output
        cells = json.loads(result.output)["scenarios"][0]["results"]
        assert cells[_HAIKU]["passed"] is False
        assert cells[_SONNET]["passed"] is True
        assert cells[_OPUS] is None


class TestTextSummary:
    def test_reports_cheapest_tier_per_scenario(self) -> None:
        result = _invoke(
            [],
            specs=[_spec("cheap_ok"), _spec("needs_sonnet")],
            passing={"cheap_ok": {_HAIKU}, "needs_sonnet": {_SONNET}},
        )
        assert result.exit_code == 0, result.output
        assert "cheap_ok" in result.output
        assert _HAIKU in result.output
        assert _SONNET in result.output

    def test_all_fail_is_surfaced_as_no_pass_not_frontier(self) -> None:
        result = _invoke([], specs=[_spec("hopeless")], passing={"hopeless": set()})
        assert result.exit_code == 0, result.output
        assert "NO-PASS" in result.output
        assert "hopeless" in result.output
        assert "passed no tier" in result.output


class TestShardSelection:
    def test_malformed_shard_exits_two(self) -> None:
        result = _invoke(["--shard", "bogus"], specs=[_spec("alpha")], passing={"alpha": {_HAIKU}})
        assert result.exit_code == 2

    def test_a_shard_runs_only_its_subset(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        result = _invoke(
            ["--shard", "1/2", "--format", "json"], specs=specs, passing={"alpha": {_HAIKU}, "beta": {_HAIKU}}
        )
        assert result.exit_code == 0, result.output
        names = [s["name"] for s in json.loads(result.output)["scenarios"]]
        assert len(names) == 1


class TestBaselineMechanism:
    """End to end: the ladder JSON, fed to ``set-baseline``, writes the cheapest-passing map."""

    def test_written_tier_map_matches_cheapest_passing_model(self, tmp_path: Path) -> None:
        specs = [_spec("cheap_ok"), _spec("needs_sonnet"), _spec("needs_opus"), _spec("hopeless")]
        passing = {"cheap_ok": {_HAIKU}, "needs_sonnet": {_SONNET}, "needs_opus": {_OPUS}, "hopeless": set()}
        ladder_json = _invoke(["--format", "json"], specs=specs, passing=passing)
        assert ladder_json.exit_code == 0, ladder_json.output
        matrix = tmp_path / "matrix.json"
        matrix.write_text(ladder_json.output, encoding="utf-8")
        out = tmp_path / "baseline.yaml"
        with patch("teatree.cli.eval.set_baseline.discover_specs", return_value=specs):
            result = CliRunner().invoke(
                app, ["eval", "set-baseline", "--from", str(matrix), "--out", str(out), "--allow-frontier"]
            )
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"cheap_ok": "cheap", "needs_sonnet": "balanced", "needs_opus": "frontier"}
        # The scenario no tier passed is surfaced, never written as a frontier tier.
        assert "hopeless" not in written["scenarios"]
        assert "WARNING hopeless" in result.output


class TestLocalHostRun:
    def test_local_runs_on_the_host_and_warns(self) -> None:
        with (
            patch("teatree.cli.eval.ladder.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.ladder.make_runner", return_value=_FakeRunner({"alpha": {_HAIKU}})),
        ):
            result = CliRunner().invoke(app, ["eval", "ladder", "--local"])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output


class TestDockerRouting:
    def test_without_local_it_routes_to_the_container(self) -> None:
        with (
            patch("teatree.cli.eval.ladder.should_route_to_docker", return_value=True),
            patch("teatree.cli.eval.ladder.run_eval_in_docker", return_value=3) as run_docker,
        ):
            result = CliRunner().invoke(app, ["eval", "ladder", "--format", "json", "--shard", "1/2", "--trials", "2"])
        # The container's exit code is propagated, and the flags reach the argv.
        assert result.exit_code == 3
        argv = run_docker.call_args.args[0]
        assert argv[0] == "ladder"
        assert argv[1:] == ["--shard", "1/2", "--trials", "2", "--max-budget-usd", "2.0", "--format", "json"]

    def test_docker_unavailable_exits_two(self) -> None:
        with (
            patch("teatree.cli.eval.ladder.should_route_to_docker", return_value=True),
            patch("teatree.cli.eval.ladder.run_eval_in_docker", side_effect=DockerUnavailableError()),
        ):
            result = CliRunner().invoke(app, ["eval", "ladder"])
        assert result.exit_code == 2
        assert "docker is not on PATH" in result.output


class TestDockerPassthrough:
    def test_default_flags_omit_optional_args(self) -> None:
        argv = _docker_passthrough(_LadderFlags(shard=None, trials=1, max_budget_usd=2.0, output_format="text"))
        assert argv == ["ladder", "--max-budget-usd", "2.0"]

    def test_all_flags_are_threaded(self) -> None:
        argv = _docker_passthrough(_LadderFlags(shard="2/6", trials=3, max_budget_usd=1.5, output_format="json"))
        assert argv == ["ladder", "--shard", "2/6", "--trials", "3", "--max-budget-usd", "1.5", "--format", "json"]
