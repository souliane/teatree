"""The doctor pages when the newest SCHEDULED CI run failed (#4477).

Nobody reviews a scheduled run, so its failure stood for eleven days. A FAIL here is what
`deploy/watchdog.sh` turns into an owner DM within the day.
"""

import json
from unittest.mock import patch

import pytest

from teatree.cli.doctor.app import run_doctor_checks
from teatree.cli.doctor.checks_scheduled_ci import check_scheduled_ci_run_health
from teatree.utils.run import CommandFailedError, TimeoutExpired

_RUN_URL = "https://github.com/souliane/teatree/actions/runs/31870626336"


def _payload(*, status: str = "completed", conclusion: str = "failure") -> str:
    return json.dumps(
        [
            {
                "databaseId": 31870626336,
                "status": status,
                "conclusion": conclusion,
                "createdAt": "2026-08-15T06:55:06Z",
                "url": _RUN_URL,
            }
        ]
    )


def _forge_answers(payload: str):
    return patch("teatree.backends.github.api.list_workflow_runs", return_value=payload)


def _forge_raises(exc: Exception):
    return patch("teatree.backends.github.api.list_workflow_runs", side_effect=exc)


class TestAFailingScheduledRunPages:
    def test_failure_is_a_fail_naming_the_run(self, capsys) -> None:
        with _forge_answers(_payload(conclusion="failure")):
            assert check_scheduled_ci_run_health() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert _RUN_URL in out
        assert "2026-08-15" in out

    def test_a_green_schedule_is_ok(self, capsys) -> None:
        with _forge_answers(_payload(conclusion="success")):
            assert check_scheduled_ci_run_health() is True
        out = capsys.readouterr().out
        assert "OK" in out
        assert "FAIL" not in out

    def test_a_run_still_in_flight_is_not_yet_a_verdict(self, capsys) -> None:
        with _forge_answers(_payload(status="in_progress", conclusion="")):
            assert check_scheduled_ci_run_health() is True
        assert "FAIL" not in capsys.readouterr().out


class TestAnUnreadableAnswerIsNeverAConfidentVerdict:
    """A read that failed must WARN UNVERIFIED — never a FAIL it cannot support, never silence."""

    @pytest.mark.parametrize(
        "exc",
        [
            CommandFailedError(["gh", "run", "list"], 1, "", "gh: no authentication token"),
            FileNotFoundError("gh"),
            TimeoutExpired(["gh", "run", "list"], 60.0),
        ],
    )
    def test_a_failed_read_warns_unverified(self, capsys, exc: Exception) -> None:
        with _forge_raises(exc):
            assert check_scheduled_ci_run_health() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "UNVERIFIED" in out
        assert "FAIL" not in out

    def test_an_unparseable_answer_warns_unverified(self, capsys) -> None:
        with _forge_answers("not json at all"):
            assert check_scheduled_ci_run_health() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "UNVERIFIED" in out
        assert "FAIL" not in out

    def test_no_scheduled_run_at_all_warns_rather_than_reporting_health(self, capsys) -> None:
        """Zero scheduled runs is not a green schedule — it is a schedule that never fired."""
        with _forge_answers("[]"):
            assert check_scheduled_ci_run_health() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "FAIL" not in out


class TestTheCheckIsWired:
    def test_the_aggregate_actually_calls_it(self) -> None:
        """A check nothing invokes reports nothing — the failure mode this ticket is about."""
        assert "check_scheduled_ci_run_health" in run_doctor_checks.__code__.co_names
