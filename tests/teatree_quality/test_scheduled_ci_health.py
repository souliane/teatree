"""Reading the newest SCHEDULED CI run from `gh run list` output (#4477).

The parse is where the honesty lives: a read that FAILED and a schedule that genuinely
has no runs are different answers, and collapsing them into one empty makes a broken
credential report as "nothing wrong".
"""

import datetime as dt
import json

import pytest

from teatree.quality.scheduled_ci_health import ScheduledRun, ScheduledRunUnreadableError, newest_scheduled_run


def _row(
    *,
    run_id: int = 31870626336,
    status: str = "completed",
    conclusion: str = "failure",
    created_at: str = "2026-08-15T06:55:06Z",
    url: str = "https://github.com/souliane/teatree/actions/runs/31870626336",
) -> dict[str, object]:
    return {
        "databaseId": run_id,
        "status": status,
        "conclusion": conclusion,
        "createdAt": created_at,
        "url": url,
    }


class TestNewestScheduledRun:
    def test_reads_the_single_row_gh_returns(self) -> None:
        run = newest_scheduled_run(json.dumps([_row()]))
        assert run == ScheduledRun(
            run_id=31870626336,
            status="completed",
            conclusion="failure",
            created_at=dt.datetime(2026, 8, 15, 6, 55, 6, tzinfo=dt.UTC),
            url="https://github.com/souliane/teatree/actions/runs/31870626336",
        )

    def test_picks_the_newest_when_gh_returns_several(self) -> None:
        payload = json.dumps(
            [
                _row(run_id=1, created_at="2026-08-13T06:55:06Z", conclusion="success"),
                _row(run_id=2, created_at="2026-08-15T06:55:06Z", conclusion="failure"),
                _row(run_id=3, created_at="2026-08-14T06:55:06Z", conclusion="success"),
            ]
        )
        newest = newest_scheduled_run(payload)
        assert newest is not None
        assert newest.run_id == 2

    def test_no_scheduled_run_yet_is_none_not_an_error(self) -> None:
        assert newest_scheduled_run("[]") is None


class TestAReadFailureIsNeverAQuietEmpty:
    """A failed read must raise; degrading it to `None` reports a broken forge as a clean schedule."""

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "not json at all",
            "{}",
            '{"runs": []}',
            "null",
        ],
    )
    def test_unparseable_payload_raises(self, payload: str) -> None:
        with pytest.raises(ScheduledRunUnreadableError):
            newest_scheduled_run(payload)

    @pytest.mark.parametrize("missing", ["databaseId", "status", "conclusion", "createdAt", "url"])
    def test_a_row_missing_a_field_raises(self, missing: str) -> None:
        row = _row()
        del row[missing]
        with pytest.raises(ScheduledRunUnreadableError):
            newest_scheduled_run(json.dumps([row]))

    def test_an_untimestampable_row_raises(self) -> None:
        with pytest.raises(ScheduledRunUnreadableError):
            newest_scheduled_run(json.dumps([_row(created_at="yesterday-ish")]))


class TestWhichConclusionsCountAsFailing:
    @pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
    def test_a_broken_run_is_failing(self, conclusion: str) -> None:
        run = newest_scheduled_run(json.dumps([_row(conclusion=conclusion)]))
        assert run is not None
        assert run.failed is True

    @pytest.mark.parametrize("conclusion", ["success", "skipped", "cancelled", "neutral"])
    def test_a_run_nobody_needs_to_act_on_is_not_failing(self, conclusion: str) -> None:
        run = newest_scheduled_run(json.dumps([_row(conclusion=conclusion)]))
        assert run is not None
        assert run.failed is False

    def test_a_run_still_in_flight_has_no_verdict_yet(self) -> None:
        """`conclusion` is empty until the run completes — an unfinished run is not a failure."""
        run = newest_scheduled_run(json.dumps([_row(status="in_progress", conclusion="")]))
        assert run is not None
        assert run.failed is False
