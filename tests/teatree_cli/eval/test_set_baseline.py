"""``t3 eval set-baseline`` — regenerate the ``baseline`` preset from a matrix run.

End-to-end through the typer CLI + the real YAML/JSON loaders (``tmp_path``
files); only ``discover_specs`` is stubbed, so the "still discovered" /
"pruned" behavior is exercised for real.
"""

import itertools
import json
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from teatree.agents.model_tiering import TIER_MODELS
from teatree.cli import app
from teatree.eval.models import EvalSpec

_HAIKU = TIER_MODELS["cheap"]
_SONNET = TIER_MODELS["balanced"]
_OPUS = TIER_MODELS["frontier"]

#: The frontier model of a matrix produced before the opus-5 bump — a column no
#: current tier maps back to. Held as a literal (not a former TIER_MODELS read)
#: because the point is a value the shipped catalog no longer contains.
_STALE_OPUS = "claude-opus-4-8"


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name, scenario="sc", agent_path="skills/code/SKILL.md", prompt="p", matchers=(), source_path=Path("x.yaml")
    )


def _cell(*, passed: bool, skipped: bool = False, errored: bool = False) -> dict[str, object]:
    return {"passed": passed, "skipped": skipped, "errored": errored, "score": 1.0 if passed else 0.0, "trials": 1}


def _write_matrix(path: Path, scenarios: dict[str, dict[str, dict[str, object] | None]]) -> None:
    payload = {
        "models": [_HAIKU, _SONNET, _OPUS],
        "scenarios": [{"name": name, "results": results} for name, results in scenarios.items()],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _invoke(args: list[str], *, discovered: list[EvalSpec]) -> object:
    with patch("teatree.cli.eval.set_baseline.discover_specs", return_value=discovered):
        return CliRunner().invoke(app, ["eval", "set-baseline", *args])


class TestCheapestPassingTier:
    def test_picks_the_cheapest_of_multiple_passing_tiers(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=True), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "balanced"}

    def test_a_cell_that_only_passes_cheap_picks_cheap(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=True), _SONNET: _cell(passed=True), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}

    def test_skipped_and_errored_cells_are_never_picked(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {
                    _HAIKU: _cell(passed=True, skipped=True),
                    _SONNET: _cell(passed=True, errored=True),
                    _OPUS: _cell(passed=True),
                }
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out), "--allow-frontier"], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "frontier"}


class TestPruning:
    def test_scenario_no_longer_discovered_is_pruned(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True)},
                "renamed_away": {_HAIKU: _cell(passed=True)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}
        assert "renamed_away" not in written["scenarios"]


class TestStableSort:
    def test_output_keys_are_sorted_regardless_of_input_order(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "zeta": {_HAIKU: _cell(passed=True)},
                "alpha": {_HAIKU: _cell(passed=True)},
                "mu": {_HAIKU: _cell(passed=True)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(
            ["--from", str(matrix), "--out", str(out)],
            discovered=[_spec("zeta"), _spec("alpha"), _spec("mu")],
        )
        assert result.exit_code == 0, result.output
        raw = out.read_text(encoding="utf-8")
        scenarios_block = raw.split("scenarios:")[1].split("frontier_ok:")[0]
        names_in_order = [line.strip().rstrip(":").rstrip() for line in scenarios_block.splitlines() if line.strip()]
        assert [n.split(":")[0] for n in names_in_order] == ["alpha", "mu", "zeta"]


class TestFrontierRefusal:
    def test_frontier_only_pass_is_refused_without_allow_frontier(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 2
        assert "--allow-frontier" in result.output
        assert not out.exists()

    def test_allow_frontier_writes_the_entry_and_frontier_ok(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out), "--allow-frontier"], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "frontier"}
        assert written["frontier_ok"] == ["alpha"]


class TestFailedEverywhere:
    def test_scenario_failing_every_tier_gets_no_entry_and_a_warning(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True)},
                "beta": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=False)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha"), _spec("beta")])
        assert result.exit_code == 0, result.output
        assert "WARNING beta" in result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}
        assert "beta" not in written["scenarios"]


class TestHeaderAndUnknownColumn:
    def test_output_carries_the_generated_header(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        assert out.read_text(encoding="utf-8").startswith("# GENERATED by t3 eval set-baseline")

    def test_header_carries_the_two_command_regen_recipe(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        header = _header_lines(out.read_text(encoding="utf-8"))
        blob = "\n".join(header)
        assert "t3 eval ladder --format json > matrix.json" in blob
        assert "t3 eval set-baseline --from matrix.json" in blob

    def test_header_explains_the_pins_and_the_declared_tier_floor(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        blob = "\n".join(_header_lines(out.read_text(encoding="utf-8")))
        assert "cheapest" in blob
        assert "floor_to_declared_tier" in blob
        assert "frontier_ok" in blob

    def test_header_is_not_reducible_to_a_one_liner(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        assert len(_header_lines(out.read_text(encoding="utf-8"))) >= 10

    def test_unrecognized_matrix_column_is_fail_loud(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        matrix.write_text(
            json.dumps(
                {
                    "models": ["some-custom-model"],
                    "scenarios": [{"name": "alpha", "results": {"some-custom-model": _cell(passed=True)}}],
                }
            ),
            encoding="utf-8",
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 2
        assert "some-custom-model" in result.output


class TestStaleModelColumn:
    """A matrix produced BEFORE a tier bump names a model no longer in ``TIER_MODELS``.

    Such a column carries no evidence about any CURRENT tier model, so its cells
    are dropped and the derivation proceeds on the columns that are still valid.
    Dropping a candidate can only raise the derived tier or remove the pin — it
    can never make a scenario look cheapest-passing at a tier it never passed —
    so the old artifact stays usable instead of aborting the whole regeneration.
    """

    def test_stale_column_is_skipped_and_the_valid_columns_still_yield_pins(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True), _SONNET: _cell(passed=True), _STALE_OPUS: _cell(passed=True)},
                "beta": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=True), _STALE_OPUS: _cell(passed=True)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha"), _spec("beta")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap", "beta": "balanced"}

    def test_the_skipped_column_is_warned_about_by_name(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True), _STALE_OPUS: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output
        assert _STALE_OPUS in result.output
        assert "t3 eval ladder" in result.output

    def test_a_pass_only_on_the_stale_column_yields_no_pin(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True)},
                "beta": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _STALE_OPUS: _cell(passed=True)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha"), _spec("beta")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}
        assert written["frontier_ok"] == []

    def test_a_wholly_stale_matrix_refuses_to_overwrite_the_baseline(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_STALE_OPUS: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        out.write_text("# pre-existing\nscenarios: {}\nfrontier_ok: []\n", encoding="utf-8")
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 2
        assert _STALE_OPUS in result.output
        assert "t3 eval ladder" in result.output
        assert out.read_text(encoding="utf-8") == "# pre-existing\nscenarios: {}\nfrontier_ok: []\n"


class TestCurrentMatrixControl:
    """A fully-current matrix derives exactly the pins it derives today."""

    def test_current_matrix_pins_are_unchanged(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True), _SONNET: _cell(passed=True), _OPUS: _cell(passed=True)},
                "beta": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=True), _OPUS: _cell(passed=True)},
                "gamma": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=True)},
                "delta": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=False)},
                "epsilon": {_HAIKU: _cell(passed=True, skipped=True), _SONNET: _cell(passed=True), _OPUS: None},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(
            ["--from", str(matrix), "--out", str(out), "--allow-frontier"],
            discovered=[_spec(name) for name in ("alpha", "beta", "gamma", "delta", "epsilon")],
        )
        assert result.exit_code == 0, result.output
        assert yaml.safe_load(out.read_text(encoding="utf-8")) == {
            "scenarios": {"alpha": "cheap", "beta": "balanced", "epsilon": "balanced", "gamma": "frontier"},
            "frontier_ok": ["gamma"],
        }
        assert "WARNING delta" in result.output


class TestOutDefaultIsResolvedAtCallTime:
    """The ``--out`` default must not bind the absolute BASELINE_PRESET_PATH.

    Binding it makes the rendered help — and therefore the committed CLI
    reference — carry the generating machine's absolute path, so docs-drift can
    never be green on two different checkouts.
    """

    def test_help_does_not_leak_an_absolute_path(self) -> None:
        result = CliRunner().invoke(app, ["eval", "set-baseline", "--help"])
        assert result.exit_code == 0
        assert "/" not in _default_marker_text(result.output)

    def test_omitting_out_writes_to_the_module_path(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True)}})
        target = tmp_path / "baseline.yaml"
        with patch("teatree.cli.eval.set_baseline.BASELINE_PRESET_PATH", target):
            result = _invoke(["--from", str(matrix)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["scenarios"] == {"alpha": "cheap"}


def _header_lines(baseline_yaml: str) -> list[str]:
    """The leading comment block of a written baseline file."""
    return list(itertools.takewhile(lambda line: line.startswith("#"), baseline_yaml.splitlines()))


def _default_marker_text(help_output: str) -> str:
    """The ``[default: ...]`` fragment of a rendered help screen, or ``""``."""
    _, _, tail = help_output.partition("[default:")
    return tail.partition("]")[0]
