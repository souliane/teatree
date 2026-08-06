"""How long ago the committed durations artifact was last refreshed (#4130)."""

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.quality.durations_freshness import (
    MAX_REFRESH_AGE,
    DurationsHistoryUnreadableError,
    _shallow_boundary_commits,
    measure_durations_freshness,
)
from tests._git_repo import make_git_repo, run_git

_NOW = dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.UTC)

CommitAt = Callable[..., None]


@pytest.fixture
def commit_at(monkeypatch: pytest.MonkeyPatch) -> CommitAt:
    """Commit with a controlled committer date — the field ``%cI`` reads.

    ``git commit --date`` moves only the AUTHOR date, so a fixture that used it
    would leave every commit stamped "now" for the measurement under test.
    """

    def _commit(repo: Path, message: str, when: dt.datetime, *, allow_empty: bool = False) -> None:
        monkeypatch.setenv("GIT_COMMITTER_DATE", when.isoformat())
        args = ["commit", "-q", "-m", message, "--date", when.isoformat()]
        if allow_empty:
            args.insert(1, "--allow-empty")
        run_git(repo, *args)

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

    def test_a_full_history_that_never_recorded_the_artifact_is_unanswerable(self, tmp_path: Path) -> None:
        """No commit anywhere in a (non-shallow) history touched the artifact."""
        repo = make_git_repo(tmp_path / "repo")
        assert measure_durations_freshness(repo, now=_NOW) is None

    def test_a_checkout_whose_history_git_refuses_is_loud_not_fresh(self, tmp_path: Path) -> None:
        """A read failure must not degrade to an empty answer that holds the alarm down forever."""
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/admin/dir\n", encoding="utf-8")

        with pytest.raises(DurationsHistoryUnreadableError):
            measure_durations_freshness(broken, now=_NOW)


def _shallow_clone_repro(tmp_path: Path, commit_at: CommitAt, *, refresh_within_fetched_depth: bool) -> Path:
    """A source repo plus a shallow clone of it, shaped to control where the fetch boundary lands.

    The source history is: root (no durations) -> durations refresh -> three unrelated commits
    -> HEAD. ``refresh_within_fetched_depth=False`` clones deep enough that the refresh commit
    itself becomes the grafted boundary (git then reports it as touching every file, including
    the durations artifact, regardless of whether it really did — the live #4130-review bug).
    ``True`` clones one commit deeper so the true root is the boundary instead, leaving the
    refresh commit reachable as ordinary (non-boundary) history.
    """
    source = make_git_repo(tmp_path / "source", initial_commit=False)
    commit_at(source, "root, no durations", dt.datetime(2025, 12, 1, tzinfo=dt.UTC), allow_empty=True)

    durations = source / "dev" / ".test_durations"
    durations.parent.mkdir(parents=True, exist_ok=True)
    durations.write_text(json.dumps({"tests/test_a.py::test_x": 1.0}), encoding="utf-8")
    run_git(source, "add", "dev/.test_durations")
    commit_at(source, "refresh durations", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))

    for day in (1, 2, 3):
        unrelated = source / "unrelated.txt"
        unrelated.write_text(f"line {day}", encoding="utf-8")
        run_git(source, "add", "unrelated.txt")
        commit_at(source, f"unrelated commit {day}", dt.datetime(2026, 2, day, tzinfo=dt.UTC))

    depth = 5 if refresh_within_fetched_depth else 3
    clone = tmp_path / "shallow_clone"
    run_git(tmp_path, "clone", "-q", "--depth", str(depth), f"file://{source}", str(clone))
    return clone


class TestShallowCloneBoundary:
    """A grafted shallow-clone boundary must never stand in for real history (#4130 review)."""

    def test_a_boundary_standing_in_for_the_real_touch_is_loud_not_fresh(
        self, tmp_path: Path, commit_at: CommitAt
    ) -> None:
        clone = _shallow_clone_repro(tmp_path, commit_at, refresh_within_fetched_depth=False)
        assert run_git(clone, "rev-parse", "--is-shallow-repository") == "true"

        with pytest.raises(DurationsHistoryUnreadableError, match="shallow-clone boundary"):
            measure_durations_freshness(clone, now=dt.datetime(2026, 2, 6, tzinfo=dt.UTC))

    def test_a_shallow_clone_whose_boundary_predates_the_real_touch_stays_trusted(
        self, tmp_path: Path, commit_at: CommitAt
    ) -> None:
        clone = _shallow_clone_repro(tmp_path, commit_at, refresh_within_fetched_depth=True)
        assert run_git(clone, "rev-parse", "--is-shallow-repository") == "true"

        freshness = measure_durations_freshness(clone, now=dt.datetime(2026, 2, 6, tzinfo=dt.UTC))
        assert freshness is not None
        assert freshness.last_refreshed_at == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        assert freshness.is_stale


class TestShallowBoundaryCommitsDefensiveEdges:
    """Branches real git cannot exercise: ``--is-shallow-repository`` already said true.

    Reserved as mocked unit tests per the repo's own doctrine (third-party subprocess) —
    an installation where ``rev-parse`` fails or the shallow file is missing right after
    git itself reported the repo shallow is not something a real fixture can construct.
    """

    def test_an_unreadable_git_common_dir_is_treated_as_no_boundary(self, tmp_path: Path) -> None:
        responses = [
            CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
            CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal: not a git repository"),
        ]
        with patch("teatree.quality.durations_freshness.run_with_status", side_effect=responses):
            assert _shallow_boundary_commits(tmp_path) == frozenset()

    def test_a_missing_shallow_file_is_treated_as_no_boundary(self, tmp_path: Path) -> None:
        responses = [
            CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
            CompletedProcess(args=[], returncode=0, stdout=f"{tmp_path}\n", stderr=""),
        ]
        with patch("teatree.quality.durations_freshness.run_with_status", side_effect=responses):
            assert _shallow_boundary_commits(tmp_path) == frozenset()
