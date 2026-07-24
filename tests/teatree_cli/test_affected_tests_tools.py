"""``t3 tool affected-tests`` — the CLI surface for the #113/#3672 selector."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.affected_tests import FORCE_KEEP_PLUGIN, Selection, SelectionReason

runner = CliRunner()

_SCOPED = Selection(
    full=False,
    reason="scoped to the diff — no FULL trigger",
    base_ref="origin/main",
    force_keep=("tests/quality", "tests/teatree_core/test_session.py"),
    doctest_targets=("src/teatree/core/session.py",),
    reasons=(
        SelectionReason(
            test="tests/teatree_core/test_session.py",
            kind="mirror",
            chain=("mirror path of teatree.core.session",),
        ),
    ),
    changed_src=("src/teatree/core/session.py",),
)
_FULL = Selection(full=True, reason="conftest changes doctest/fixture semantics tree-wide (tests/conftest.py)")


class TestOutputModes:
    def test_default_prints_human_report(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests"])
        assert result.exit_code == 0
        assert "affected-tests: SCOPED" in result.output
        assert "reason:" in result.output

    def test_json_emits_selection(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["full"] is False
        assert "tests/teatree_core/test_session.py" in payload["force_keep"]
        assert "--tach" in payload["pytest_args"]
        assert "tach_advisory" not in payload  # the #3672 advisory scaffolding is gone

    def test_pytest_args_activates_the_plugin(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--pytest-args"])
        assert result.exit_code == 0
        assert "--tach" in result.output
        assert FORCE_KEEP_PLUGIN in result.output

    def test_explain_traces_a_force_kept_test(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--explain", "tests/teatree_core/test_session.py"])
        assert result.exit_code == 0
        assert "mirror path of teatree.core.session" in result.output

    def test_explain_all_lists_every_force_kept_test(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--explain", "all"])
        assert result.exit_code == 0
        assert "tests/teatree_core/test_session.py [mirror]" in result.output

    def test_full_report_names_the_trigger(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_FULL):
            result = runner.invoke(app, ["tool", "affected-tests"])
        assert result.exit_code == 0
        assert "affected-tests: FULL" in result.output
        assert "conftest" in result.output

    def test_full_pytest_args_has_no_tach_flag(self) -> None:
        # A FULL verdict runs the whole suite with the plugin off.
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_FULL):
            result = runner.invoke(app, ["tool", "affected-tests", "--pytest-args"])
        assert result.exit_code == 0
        assert "--tach" not in result.output

    def test_never_exits_non_zero(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_FULL):
            result = runner.invoke(app, ["tool", "affected-tests", "--pytest-args"])
        assert result.exit_code == 0
