"""How long ago the committed durations artifact was last refreshed (#4130)."""

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from teatree.quality.durations_freshness import (
    MAX_REFRESH_AGE,
    DurationsHistoryUnreadableError,
    measure_durations_freshness,
)
from tests._git_repo import make_git_repo, run_git

_NOW = dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.UTC)

CommitAt = Callable[[Path, str, dt.datetime], None]


@pytest.fixture
def commit_at(monkeypatch: pytest.MonkeyPatch) -> CommitAt:
    """Commit with a controlled committer date — the field ``%cI`` reads.

    ``git commit --date`` moves only the AUTHOR date, so a fixture that used it
    would leave every commit stamped "now" for the measurement under test.
    """

    def _commit(repo: Path, message: str, when: dt.datetime) -> None:
        monkeypatch.setenv("GIT_COMMITTER_DATE", when.isoformat())
        run_git(repo, "commit", "-q", "-m", message, "--date", when.isoformat())

    return _commit


def _repo_with_durations(tmp_path: Path, commit_at: CommitAt, when: dt.datetime) -> Path:
    repo = make_git_repo(tmp_path / "repo")
    durations = repo / "dev" / ".test_durations"
    durations.parent.mkdir(parents=True, exist_ok=True)
    durations.write_text(json.dumps({"tests/test_a.py::test_x": 1.0}), encoding="utf-8")
    run_git(repo, "add", "dev/.test_durations")
    commit_at(repo, "refresh durations", when)
    return repo


class TestMeasureDurationsFreshness:
    def test_a_recent_refresh_is_fresh(self, tmp_path: Path, commit_at: CommitAt) -> None:
        repo = _repo_with_durations(tmp_path, commit_at, _NOW - dt.timedelta(days=2))
        freshness = measure_durations_freshness(repo, now=_NOW)
        assert freshness is not None
        assert freshness.age.days == 2
        assert not freshness.is_stale

    def test_past_the_threshold_is_stale(self, tmp_path: Path, commit_at: CommitAt) -> None:
        repo = _repo_with_durations(tmp_path, commit_at, _NOW - MAX_REFRESH_AGE - dt.timedelta(days=1))
        freshness = measure_durations_freshness(repo, now=_NOW)
        assert freshness is not None
        assert freshness.is_stale

    def test_exactly_at_the_threshold_is_stale(self, tmp_path: Path, commit_at: CommitAt) -> None:
        """Inclusive boundary: at N days the artifact has gone N days without a refresh."""
        repo = _repo_with_durations(tmp_path, commit_at, _NOW - MAX_REFRESH_AGE)
        freshness = measure_durations_freshness(repo, now=_NOW)
        assert freshness is not None
        assert freshness.is_stale

    def test_a_later_unrelated_commit_does_not_count_as_a_refresh(self, tmp_path: Path, commit_at: CommitAt) -> None:
        """Only a commit that TOUCHED the artifact refreshes it — else any merge clears the alarm."""
        repo = _repo_with_durations(tmp_path, commit_at, _NOW - dt.timedelta(days=30))
        (repo / "unrelated.txt").write_text("x", encoding="utf-8")
        run_git(repo, "add", "unrelated.txt")
        commit_at(repo, "unrelated", _NOW - dt.timedelta(hours=1))

        freshness = measure_durations_freshness(repo, now=_NOW)
        assert freshness is not None
        assert freshness.age.days == 30
        assert freshness.is_stale

    def test_not_a_checkout_is_unanswerable_not_a_verdict(self, tmp_path: Path) -> None:
        assert measure_durations_freshness(tmp_path, now=_NOW) is None

    def test_a_history_that_never_recorded_the_artifact_is_unanswerable(self, tmp_path: Path) -> None:
        """A shallow clone whose depth predates the artifact must not read as freshly refreshed."""
        repo = make_git_repo(tmp_path / "repo")
        assert measure_durations_freshness(repo, now=_NOW) is None

    def test_a_checkout_whose_history_git_refuses_is_loud_not_fresh(self, tmp_path: Path) -> None:
        """A read failure must not degrade to an empty answer that holds the alarm down forever."""
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/admin/dir\n", encoding="utf-8")

        with pytest.raises(DurationsHistoryUnreadableError):
            measure_durations_freshness(broken, now=_NOW)
