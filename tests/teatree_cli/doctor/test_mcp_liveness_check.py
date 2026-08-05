"""``_check_teatree_mcp_liveness`` — the EXERCISING, hard-FAIL MCP gate (#4049).

The pre-existing registration check is a WARN by design, and a WARN is what let a
dead MCP server survive a whole session: one advisory line among ~twenty, most of the
others benign host/container artifacts. These pin the two properties that make this
check different — it FAILS (returns ``False``, so it gates ``t3 doctor check``'s exit
code) and it EXERCISES (a registration that exists proves nothing; that night the
server was registered and dead).
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.doctor.checks_mcp import _check_teatree_mcp_liveness
from teatree.core.mcp_liveness import McpExerciseOutcome, McpFailureCause
from teatree.utils.dep_skew import VersionSkew

_LIVENESS = "teatree.cli.doctor.checks_mcp"


@pytest.fixture
def no_skew(tmp_path: Path):
    """A resolvable, current source tree — so the exercise verdict is what is under test."""
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = []\n", encoding="utf-8")
    with (
        patch("teatree.utils.dep_drift.editable_source_path", return_value=tmp_path),
        patch("teatree.utils.dep_skew.find_version_skew", return_value=[]),
        patch(f"{_LIVENESS}.shutil.which", return_value="/usr/bin/t3"),
    ):
        yield tmp_path


def _exercise(outcome: McpExerciseOutcome):
    return patch("teatree.core.mcp_liveness.exercise_mcp_server", return_value=outcome)


class TestADeadServerIsAHardFail:
    @pytest.mark.parametrize(
        ("cause", "expected_remedy_fragment"),
        [
            (McpFailureCause.STALE_TOOL_ENV, "uv tool install --editable"),
            (McpFailureCause.DELEGATION_FAILURE, "delegation failure"),
            (McpFailureCause.SLOW_STARTUP, "AppConfig.ready()"),
        ],
    )
    def test_each_cause_fails_and_names_its_remedy(
        self, no_skew: Path, capsys, cause: McpFailureCause, expected_remedy_fragment: str
    ) -> None:
        outcome = McpExerciseOutcome(ok=False, elapsed=3.0, cause=cause, stderr_excerpt="the real trace")
        with _exercise(outcome):
            result = _check_teatree_mcp_liveness()

        out = capsys.readouterr().out
        assert result is False, "an unusable MCP server must gate the doctor exit code"
        assert "FAIL" in out
        assert "WARN" not in out, "this finding must not be scrollable-past advice"
        assert expected_remedy_fragment in out

    def test_the_captured_stderr_is_printed(self, no_skew: Path, capsys) -> None:
        """`claude mcp list` only ever says `Connection closed` — the trace is the deliverable."""
        outcome = McpExerciseOutcome(
            ok=False,
            elapsed=2.0,
            cause=McpFailureCause.DELEGATION_FAILURE,
            stderr_excerpt="OperationalError: unable to open database file",
        )
        with _exercise(outcome):
            _check_teatree_mcp_liveness()

        assert "unable to open database file" in capsys.readouterr().out

    def test_a_usable_server_passes(self, no_skew: Path, capsys) -> None:
        with _exercise(McpExerciseOutcome(ok=True, elapsed=1.2)):
            assert _check_teatree_mcp_liveness() is True

        assert "FAIL" not in capsys.readouterr().out


class TestVersionSkewIsFailedAndRepaired:
    def test_skew_fails_and_names_the_side_it_found(self, no_skew: Path, capsys) -> None:
        skew = [VersionSkew(name="mcp", declared=">=2,<3", installed="1.28.1")]
        with (
            patch("teatree.utils.dep_skew.find_version_skew", return_value=skew),
            patch(f"{_LIVENESS}._repair_version_skew", return_value=False),
            _exercise(McpExerciseOutcome(ok=True, elapsed=1.0)),
        ):
            result = _check_teatree_mcp_liveness()

        out = capsys.readouterr().out
        assert result is False, "an env drifted from pyproject.toml must gate the exit code"
        assert "FAIL" in out
        assert "mcp declares '>=2,<3' but 1.28.1 is installed" in out
        assert "HOST tool env" in out or "CONTAINER env" in out, "the operator must know which side is stale"

    def test_a_successful_self_repair_clears_the_failure(self, no_skew: Path) -> None:
        """A stale env is mechanical — repaired, not escalated."""
        skew = [VersionSkew(name="mcp", declared=">=2,<3", installed="1.28.1")]
        with (
            patch("teatree.utils.dep_skew.find_version_skew", return_value=skew),
            patch(f"{_LIVENESS}._repair_version_skew", return_value=True) as repair,
            _exercise(McpExerciseOutcome(ok=True, elapsed=1.0)),
        ):
            assert _check_teatree_mcp_liveness() is True

        assert repair.call_count == 1


class TestOnlyAnUnrunnableCheckDegrades:
    def test_no_t3_on_path_warns_rather_than_failing(self, tmp_path: Path, capsys) -> None:
        with (
            patch("teatree.utils.dep_drift.editable_source_path", return_value=None),
            patch(f"{_LIVENESS}.shutil.which", return_value=None),
        ):
            assert _check_teatree_mcp_liveness() is True

        assert "WARN" in capsys.readouterr().out

    def test_an_unspawnable_server_warns_rather_than_failing(self, no_skew: Path, capsys) -> None:
        with patch("teatree.core.mcp_liveness.exercise_mcp_server", side_effect=OSError("no fork")):
            assert _check_teatree_mcp_liveness() is True

        assert "WARN" in capsys.readouterr().out
