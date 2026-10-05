"""`t3 ticket sweep-begin/sweep-finish/sweep-trend` — the CLI face of TicketSweepRun (#162 Rule 4).

The model-layer manager is pinned in ``test_ticket_sweep_run.py``; these pin the
CLI wrapper's ``--json`` machine-output contract and its error paths.
"""

import io
import json
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase


def _run(*args: str, **kwargs: object) -> dict[str, object]:
    out, err = io.StringIO(), io.StringIO()
    call_command("ticket", *args, "--json", stdout=out, stderr=err, **kwargs)
    return cast("dict[str, object]", json.loads(out.getvalue()))


def _refused(*args: str, **kwargs: object) -> dict[str, object]:
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(SystemExit) as caught:
        call_command("ticket", *args, "--json", stdout=out, stderr=err, **kwargs)
    assert caught.value.code == 1
    return cast("dict[str, object]", json.loads(out.getvalue()))


class SweepBeginCommandTest(TestCase):
    def test_opens_a_run_and_reports_its_id(self) -> None:
        result = _run("sweep-begin", source="interactive", overlay="t3-teatree")
        assert result["source"] == "interactive"
        assert result["finished"] is False
        assert result["run_id"]

    def test_an_invalid_source_is_refused(self) -> None:
        result = _refused("sweep-begin", source="not-a-real-source")
        assert "source must be one of" in str(result["error"])


class SweepFinishCommandTest(TestCase):
    def test_closes_a_run_and_persists_its_counts(self) -> None:
        begun = _run("sweep-begin", source="interactive")
        run_id = cast("str", begun["run_id"])

        result = _run("sweep-finish", run_id, examined=5, external_skipped=1)

        assert result == {
            "run_id": run_id,
            "source": "interactive",
            "changed_count": 0,
            "examined_count": 5,
            "external_skipped_count": 1,
            "finished": True,
        }

    def test_an_unknown_run_id_is_refused(self) -> None:
        result = _refused("sweep-finish", "not-a-real-run")
        assert "sweep-finish refused" in str(result["error"])


class SweepTrendCommandTest(TestCase):
    def test_an_empty_history_reports_zero_streak_and_no_runs(self) -> None:
        result = _run("sweep-trend")
        assert result == {
            "latest_changed_count": 0,
            "recent_changed_counts": [],
            "zero_streak": 0,
            "incomplete_runs": [],
        }

    def test_an_unfinished_run_is_reported_incomplete(self) -> None:
        begun = _run("sweep-begin", source="interactive")
        result = _run("sweep-trend")
        assert result["incomplete_runs"] == [begun["run_id"]]
