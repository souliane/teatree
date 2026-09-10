"""An absent SWEEP_POLICY is said out loud rather than silently defaulting (#4677).

The sweep skill reads `SWEEP_POLICY` from `~/.ac-reviewing-codebase` and, finding
nothing, defaults every repo to `bulk-update` — refresh branches, never merge. So a
sweep silently does half what the operator expects on a repo they own, with no
indication that a config was missing rather than deliberately set.
"""

from pathlib import Path

import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_sweep_policy import _check_sweep_policy_declared


def _run(config: Path) -> tuple[bool, str]:
    holder: dict[str, bool] = {}
    app = typer.Typer()

    @app.command()
    def run() -> None:
        holder["ok"] = _check_sweep_policy_declared(config_path=config)

    result = CliRunner().invoke(app, [])
    return holder["ok"], result.output


class TestAnAbsentPolicyIsReported:
    def test_a_missing_config_file_is_named(self, tmp_path: Path) -> None:
        ok, output = _run(tmp_path / ".ac-reviewing-codebase")

        assert ok
        assert "INFO" in output
        assert "SWEEP_POLICY" in output
        assert ".ac-reviewing-codebase" in output

    def test_the_defaulting_behaviour_is_stated_rather_than_left_implicit(self, tmp_path: Path) -> None:
        _, output = _run(tmp_path / ".ac-reviewing-codebase")

        assert "bulk-update" in output

    def test_a_config_carrying_no_policy_is_reported_too(self, tmp_path: Path) -> None:
        config = tmp_path / ".ac-reviewing-codebase"
        config.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")

        ok, output = _run(config)

        assert ok
        assert "SWEEP_POLICY" in output

    def test_the_finding_never_gates(self, tmp_path: Path) -> None:
        ok, output = _run(tmp_path / ".ac-reviewing-codebase")

        # The file is an owner preference, so its absence is a recommendation.
        assert ok
        assert "FAIL" not in output


class TestADeclaredPolicyIsSilent:
    def test_a_config_declaring_a_policy_produces_no_finding(self, tmp_path: Path) -> None:
        config = tmp_path / ".ac-reviewing-codebase"
        config.write_text('SWEEP_POLICY="souliane/.+:serial-merge"\n', encoding="utf-8")

        ok, output = _run(config)

        assert ok
        assert output.strip() == ""

    def test_a_commented_out_policy_does_not_count_as_declared(self, tmp_path: Path) -> None:
        config = tmp_path / ".ac-reviewing-codebase"
        config.write_text('# SWEEP_POLICY="souliane/.+:serial-merge"\n', encoding="utf-8")

        ok, output = _run(config)

        assert ok
        assert "SWEEP_POLICY" in output
