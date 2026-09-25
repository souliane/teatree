"""The advisory net hand-written LoC report — what it counts, and what it never fails on.

Advisory by owner ruling: a blocking net-LoC gate was considered and rejected as
too much friction, so every case here also asserts the exit code stays 0.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teatree.paths import teatree_source_root
from teatree.quality.hand_written_loc import diff_range_loc, window_loc
from teatree.utils.run import run_allowed_to_fail, run_checked
from tests._git_repo import git_identity_env, make_git_repo, run_git


def _commit(repo: Path, message: str) -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


def _commit_dated(repo: Path, message: str, when: str) -> None:
    run_git(repo, "add", "-A")
    run_checked(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        env={**git_identity_env(), "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    checkout = make_git_repo(tmp_path / "repo")
    (checkout / "kept.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    (checkout / "docs" / "generated").mkdir(parents=True)
    (checkout / "docs" / "generated" / "cli-reference.md").write_text("x\n", encoding="utf-8")
    _commit(checkout, "base")
    run_git(checkout, "checkout", "-q", "-b", "work")
    return checkout


class TestDiffRangeLoc:
    def test_net_positive_range_reports_a_positive_net(self, repo: Path) -> None:
        (repo / "kept.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
        _commit(repo, "add two lines")

        delta = diff_range_loc("main", "HEAD", repo=repo)

        assert (delta.added, delta.deleted, delta.net) == (2, 0, 2)

    def test_net_negative_range_reports_a_negative_net(self, repo: Path) -> None:
        (repo / "kept.py").write_text("a\n", encoding="utf-8")
        _commit(repo, "delete three lines")

        assert diff_range_loc("main", "HEAD", repo=repo).net == -3

    def test_generated_paths_are_not_hand_written(self, repo: Path) -> None:
        (repo / "docs" / "generated" / "cli-reference.md").write_text("x\ny\nz\n", encoding="utf-8")
        _commit(repo, "regenerate")

        delta = diff_range_loc("main", "HEAD", repo=repo)

        assert (delta.added, delta.deleted, delta.net) == (0, 0, 0)

    def test_merge_generated_attribute_excludes_a_path_no_glob_names(self, repo: Path) -> None:
        (repo / ".gitattributes").write_text("build/report.md merge=generated\n", encoding="utf-8")
        (repo / "build").mkdir()
        (repo / "build" / "report.md").write_text("x\ny\n", encoding="utf-8")
        _commit(repo, "generated artifact under a path no glob names")

        assert diff_range_loc("main", "HEAD", repo=repo).added == 1


class TestTheAdvisoryScript:
    """``scripts/ci/loc_ratchet.py`` reports the number and never fails a pipeline."""

    def _run(self, repo: Path) -> tuple[int, str]:
        script = teatree_source_root() / "scripts" / "ci" / "loc_ratchet.py"
        done = run_allowed_to_fail(
            [sys.executable, str(script), "main", "HEAD"],
            expected_codes=None,
            cwd=repo,
        )
        return done.returncode, done.stdout.strip()

    def test_growth_prints_a_positive_net_and_still_exits_zero(self, repo: Path) -> None:
        (repo / "kept.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
        _commit(repo, "add two lines")

        assert self._run(repo) == (0, "hand-written LoC +2 -0 = net +2")

    def test_shrink_prints_a_negative_net_and_still_exits_zero(self, repo: Path) -> None:
        (repo / "kept.py").write_text("a\n", encoding="utf-8")
        _commit(repo, "delete three lines")

        assert self._run(repo) == (0, "hand-written LoC +0 -3 = net -3")

    def test_a_generated_only_diff_prints_zero_and_still_exits_zero(self, repo: Path) -> None:
        (repo / "docs" / "generated" / "cli-reference.md").write_text("x\ny\nz\n", encoding="utf-8")
        _commit(repo, "regenerate")

        assert self._run(repo) == (0, "hand-written LoC +0 -0 = net +0")


class TestWindowLoc:
    """The dashboard leg: the tree delta between the window's two boundary commits."""

    def test_reports_the_trees_net_and_the_windows_commit_count(self, repo: Path) -> None:
        (repo / "kept.py").write_text("a\nb\n", encoding="utf-8")
        _commit(repo, "shrink to two lines")
        now = datetime.now(UTC)

        delta = window_loc(since=now - timedelta(minutes=5), until=now + timedelta(minutes=5), repo=repo)

        assert (delta.added, delta.deleted, delta.commits) == (2, 0, 3)

    def test_a_window_reaching_the_first_commit_diffs_against_the_empty_tree(self, repo: Path) -> None:
        now = datetime.now(UTC)

        delta = window_loc(since=now - timedelta(days=1), until=now + timedelta(minutes=5), repo=repo)

        assert (delta.added, delta.deleted) == (4, 0)

    def test_a_window_with_no_commits_is_zero(self, repo: Path) -> None:
        now = datetime.now(UTC)

        delta = window_loc(since=now - timedelta(days=2), until=now - timedelta(days=1), repo=repo)

        assert (delta.added, delta.deleted, delta.commits) == (0, 0, 0)

    def test_a_window_between_two_boundary_commits_sees_only_that_span(self, tmp_path: Path) -> None:
        checkout = make_git_repo(tmp_path / "dated")
        for month, body in (("01", "a\n"), ("02", "a\nb\nc\n"), ("03", "a\nb\nc\nd\ne\n")):
            (checkout / "kept.py").write_text(body, encoding="utf-8")
            _commit_dated(checkout, f"2020-{month}", f"2020-{month}-01T12:00:00+00:00")

        delta = window_loc(
            since=datetime(2020, 1, 15, tzinfo=UTC),
            until=datetime(2020, 2, 15, tzinfo=UTC),
            repo=checkout,
        )

        assert (delta.added, delta.deleted, delta.commits) == (2, 0, 1)
