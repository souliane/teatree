"""When a refresh of ``dev/.test_durations`` last landed — the age behind the FAIL (#4130)."""

import datetime as dt
import json
from pathlib import Path

import pytest

from teatree.quality.durations_freshness import MAX_REFRESH_AGE_DAYS, measure_durations_freshness
from tests._git_repo import make_git_repo, run_git


def _commit_durations(repo: Path, *, days_ago: int, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "dev").mkdir(exist_ok=True)
    (repo / "dev" / ".test_durations").write_text(json.dumps({"tests/test_a.py::test_x": 1.0}), encoding="utf-8")
    # Landing a few hours inside the day keeps the age an exact whole number of days:
    # `git` only accepts a strict timestamp here, and a midnight one would round by the
    # clock rather than by the elapsed interval the check measures.
    landed = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago, hours=1)
    monkeypatch.setenv("GIT_COMMITTER_DATE", landed.isoformat())
    monkeypatch.setenv("GIT_AUTHOR_DATE", landed.isoformat())
    run_git(repo, "add", "dev/.test_durations")
    run_git(repo, "commit", "-q", "-m", "chore(ci): refresh dev/.test_durations")
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    monkeypatch.delenv("GIT_AUTHOR_DATE")


class TestMeasureDurationsFreshness:
    def test_a_refresh_that_landed_today_is_fresh(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_git_repo(tmp_path)
        _commit_durations(repo, days_ago=0, monkeypatch=monkeypatch)

        freshness = measure_durations_freshness(repo)

        assert freshness is not None
        assert freshness.age_days == 0
        assert not freshness.is_stale

    def test_the_last_day_inside_the_window_is_still_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo(tmp_path)
        _commit_durations(repo, days_ago=MAX_REFRESH_AGE_DAYS - 1, monkeypatch=monkeypatch)

        freshness = measure_durations_freshness(repo)

        assert freshness is not None
        assert freshness.age_days == MAX_REFRESH_AGE_DAYS - 1
        assert not freshness.is_stale

    def test_the_window_itself_is_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_git_repo(tmp_path)
        _commit_durations(repo, days_ago=MAX_REFRESH_AGE_DAYS, monkeypatch=monkeypatch)

        freshness = measure_durations_freshness(repo)

        assert freshness is not None
        assert freshness.is_stale

    def test_a_long_dead_pipeline_reports_its_age(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_git_repo(tmp_path)
        _commit_durations(repo, days_ago=40, monkeypatch=monkeypatch)

        freshness = measure_durations_freshness(repo)

        assert freshness is not None
        assert freshness.age_days == 40
        assert freshness.is_stale

    def test_an_absent_durations_file_is_unanswerable_never_a_verdict(self, tmp_path: Path) -> None:
        """A FAIL pages the owner, so an age nobody established must never produce one."""
        assert measure_durations_freshness(make_git_repo(tmp_path)) is None

    def test_a_tree_that_is_not_a_checkout_is_unanswerable(self, tmp_path: Path) -> None:
        (tmp_path / "dev").mkdir()
        (tmp_path / "dev" / ".test_durations").write_text("{}", encoding="utf-8")

        assert measure_durations_freshness(tmp_path) is None

    def test_a_history_that_never_recorded_the_file_is_unanswerable(self, tmp_path: Path) -> None:
        """A shallow or grafted clone can hold the file with no commit that touched it."""
        repo = make_git_repo(tmp_path)
        (repo / "dev").mkdir()
        (repo / "dev" / ".test_durations").write_text("{}", encoding="utf-8")

        assert measure_durations_freshness(repo) is None

    def test_a_git_that_cannot_run_is_unanswerable_never_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo(tmp_path)
        _commit_durations(repo, days_ago=0, monkeypatch=monkeypatch)
        monkeypatch.setattr(
            "teatree.quality.durations_freshness.run_allowed_to_fail",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no git here")),
        )

        assert measure_durations_freshness(repo) is None

    def test_an_unparseable_landing_date_is_unanswerable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_git_repo(tmp_path)
        _commit_durations(repo, days_ago=0, monkeypatch=monkeypatch)
        monkeypatch.setattr(
            "teatree.quality.durations_freshness._last_landing_iso",
            lambda _repo: "not-a-date",
        )

        assert measure_durations_freshness(repo) is None


class TestTheWindowItself:
    def test_the_window_sits_between_the_longest_healthy_silence_and_a_fortnight(self) -> None:
        """The band the derivation fixes; drifting out of it re-creates one of the two failures.

        Below 10 a full unattended week with the refresh PR open but unmerged — a healthy
        pipeline — pages on the day the owner returns, which is the nightly page #4113
        removed. At 14 the file has already decayed past the coverage floor by age alone,
        so the alarm arrives after the blindness it exists to prevent.
        """
        longest_healthy_silence = 7 + 1 + 1  # unattended week + a cron cycle + the merge on return
        assert longest_healthy_silence < MAX_REFRESH_AGE_DAYS < 14
