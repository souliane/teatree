"""``t3 tool affected-tests`` — the CLI surface for the #113 selector."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.affected_tests import FLOOR_DIRS, Selection, SelectionReason

runner = CliRunner()

_SCOPED = Selection(
    full=False,
    reason="scoped to the diff — no FULL trigger",
    test_files=("tests/teatree_core/test_session.py",),
    floor_dirs=FLOOR_DIRS,
    doctest_targets=("src/teatree/core/session.py",),
    reasons=(
        SelectionReason(
            test="tests/teatree_core/test_session.py",
            kind="import-match",
            chain=(
                "src/teatree/core/session.py (changed)",
                "tests/teatree_core/test_session.py imports teatree.core.session",
            ),
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
        assert "tests/teatree_core/test_session.py" in payload["test_files"]
        assert payload["pytest_args"][-len(FLOOR_DIRS) :] == list(FLOOR_DIRS)

    def test_pytest_args_emits_positional_args(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--pytest-args"])
        assert result.exit_code == 0
        assert "tests/teatree_core/test_session.py" in result.output
        for floor in FLOOR_DIRS:
            assert floor in result.output

    def test_explain_traces_a_selected_test(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--explain", "tests/teatree_core/test_session.py"])
        assert result.exit_code == 0
        assert "src/teatree/core/session.py (changed)" in result.output
        assert "imports teatree.core.session" in result.output

    def test_explain_all_lists_every_selected_test(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_SCOPED):
            result = runner.invoke(app, ["tool", "affected-tests", "--explain", "all"])
        assert result.exit_code == 0
        assert "tests/teatree_core/test_session.py [import-match]" in result.output

    def test_full_report_names_the_trigger(self) -> None:
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_FULL):
            result = runner.invoke(app, ["tool", "affected-tests"])
        assert result.exit_code == 0
        assert "affected-tests: FULL" in result.output
        assert "conftest" in result.output

    def test_never_exits_non_zero(self) -> None:
        # Informational only: the tool never gates a push, so it never exits non-zero.
        with patch("teatree.cli.affected_tests_tools.build_selection", return_value=_FULL):
            result = runner.invoke(app, ["tool", "affected-tests", "--pytest-args"])
        assert result.exit_code == 0
