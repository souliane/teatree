"""Session-end unshipped-work backstop — the probes behind each of the five work-bearing states.

Staged and unstaged changes are decided by an index-aware probe, unpushed commits against the
branch's own upstream, and a probe that could not answer is ``None``, never an empty answer:
only an answer may clear an earlier report (``test_session_end_work_check``).
"""

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.session_end_work_check as work_check
from hooks.scripts import stranded_work_report
from tests._session_end_harness import GIT as _GIT
from tests._session_end_harness import context as _context
from tests._session_end_harness import git as _git
from tests._session_end_harness import repo_with_commit as _repo_with_commit
from tests._session_end_harness import run_end as _run
from tests._session_end_harness import session_end_sandbox

#: Captured before the sandbox patches it, so this module can exercise the real probe.
_REAL_FETCH_ORPHANS = work_check.fetch_orphans


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with session_end_sandbox(tmp_path, monkeypatch):
        yield


def _upstream(repo: Path, bare: Path) -> None:
    """Give *repo*'s branch a real tracking upstream in a local bare remote."""
    subprocess.run([_GIT, "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "work-branch")


@contextlib.contextmanager
def _fake_t3(*, returncode: int, stdout: str):
    completed = subprocess.CompletedProcess(args=["t3"], returncode=returncode, stdout=stdout, stderr="")
    with (
        patch.object(work_check, "t3_argv", return_value=["/usr/bin/t3", "teatree", "workspace", "list-orphans"]),
        patch.object(work_check, "run_t3", return_value=completed),
    ):
        yield


class TestDirtyWorktreeStates:
    """States 1-3 — decided by an index-aware probe, never a bare ``git diff``."""

    def test_staged_but_uncommitted_work_is_reported(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        (repo / "b.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "b.txt")

        ctx = _context({"session_id": "s-staged", "cwd": str(repo)})

        assert "staged" in ctx
        assert str(repo) in ctx
        assert "git -C" in ctx
        assert "commit" in ctx

    def test_unstaged_work_is_reported(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")

        ctx = _context({"session_id": "s-unstaged", "cwd": str(repo)})

        assert "unstaged" in ctx
        assert "git -C" in ctx

    def test_unpushed_commits_are_reported(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)

        ctx = _context({"session_id": "s-unpushed", "cwd": str(repo)})

        assert "unpushed" in ctx
        assert "push" in ctx
        assert "work-branch" in ctx

    def test_a_session_that_ended_in_a_subdirectory_reports_its_checkout(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        (repo / "pkg").mkdir()

        assert _run({"session_id": "s-subdir", "cwd": str(repo / "pkg")}) == (None, "", "")

        report = stranded_work_report.claim(str(repo)).text
        assert "unpushed" in report
        assert "work-branch" in report

    def test_staged_work_in_a_repo_with_no_commit_yet_is_reported(self, tmp_path: Path) -> None:
        # No HEAD yet is git's answer, not a failed probe: the work is still stranded.
        repo = tmp_path / "fresh"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "first-branch")
        (repo / "b.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "b.txt")

        scan = work_check.scan_work(str(repo))

        assert scan.complete is True
        assert [item.state for item in scan.items] == ["staged"]
        assert "first-branch" in scan.items[0].label

    def test_a_detached_head_is_named_as_such(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        _git(repo, "checkout", "-q", "--detach")

        assert work_check.current_branch(repo) == "(detached)"

    def test_clean_synced_repo_reports_nothing(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        with patch.object(work_check, "unpushed_commit_count", return_value=0):
            assert _run({"session_id": "s-clean-repo", "cwd": str(repo)}) == (None, "", "")


class TestOpenPullRequest:
    def test_open_pr_authored_by_this_session_is_reported(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        prs = [{"number": 42, "title": "Add the thing", "headRefName": "work-branch"}]
        with (
            patch.object(work_check, "read_open_prs", return_value=prs),
            patch.object(work_check, "unpushed_commit_count", return_value=0),
        ):
            ctx = _context({"session_id": "s-pr", "cwd": str(repo)})

        assert "#42" in ctx
        assert "Add the thing" in ctx
        assert "loops tick --loop ship" in ctx


class TestIndexAwareDirtinessProbe:
    """Defect B: bare ``git diff`` returns 0 bytes against staged-only work."""

    def test_staged_only_work_reads_dirty(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        (repo / "b.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "b.txt")

        assert work_check.staged_paths(repo) == ["b.txt"]
        assert work_check.unstaged_paths(repo) == []

    def test_a_truncated_porcelain_row_is_ignored(self) -> None:
        with patch.object(work_check, "_git", return_value="M\n M real.txt"):
            assert work_check._porcelain_rows(Path("/x")) == [(" ", "M", "real.txt")]

    def test_an_unstaged_only_repo_reports_the_exact_path(self, tmp_path: Path) -> None:
        # ``git status --porcelain`` puts the worktree code in column 2, so the row's
        # LEADING space is data: stripping it shifts every column and mangles the path.
        repo = _repo_with_commit(tmp_path)
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")

        assert work_check.unstaged_paths(repo) == ["a.txt"]
        assert work_check.staged_paths(repo) == []

    def test_a_long_path_list_is_previewed(self) -> None:
        rendered = work_check._names([f"f{i}.txt" for i in range(9)])

        assert rendered.endswith(f"+{9 - work_check.PREVIEW_LIMIT} more")


class TestUnpushedCounting:
    def test_commits_ahead_of_a_configured_upstream_count(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        _upstream(repo, tmp_path / "origin.git")
        (repo / "c.txt").write_text("second\n", encoding="utf-8")
        _git(repo, "add", "c.txt")
        _git(repo, "commit", "-qm", "second")

        assert work_check.unpushed_commit_count(repo) == 1

    def test_commits_ahead_of_the_upstream_count_even_when_another_remote_branch_has_them(self, tmp_path: Path) -> None:
        # The branch's own upstream is the question; a copy pushed elsewhere does not ship it.
        repo = _repo_with_commit(tmp_path)
        _upstream(repo, tmp_path / "origin.git")
        (repo / "c.txt").write_text("second\n", encoding="utf-8")
        _git(repo, "add", "c.txt")
        _git(repo, "commit", "-qm", "second")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/elsewhere")

        assert work_check.unpushed_commit_count(repo) == 1

    @pytest.mark.parametrize("ref", ["HEAD", "@{u}"])
    def test_a_ref_query_that_errors_is_a_failed_probe_not_an_absent_ref(self, tmp_path: Path, ref: str) -> None:
        # Only the quiet query's "no such ref" exit is an answer; 128 (a repo git cannot read) is not.
        repo = _repo_with_commit(tmp_path)
        real = subprocess.check_output

        def unreadable_ref(argv: list[str], **_kwargs: object) -> str:
            if argv[-1] == ref and "--verify" in argv:
                raise subprocess.CalledProcessError(128, argv)
            return real(argv, text=True, stderr=subprocess.DEVNULL)

        with (
            patch.object(work_check.subprocess, "check_output", side_effect=unreadable_ref),
            pytest.raises(work_check.ProbeFailedError),
        ):
            work_check.unpushed_commit_count(repo)

    def test_a_synced_upstream_counts_zero(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        _upstream(repo, tmp_path / "origin.git")

        assert work_check.unpushed_commit_count(repo) == 0


class TestFetchOrphans:
    """A probe that could not answer is ``None``, never an empty answer: only an answer may clear a report."""

    def test_a_missing_t3_binary_is_no_answer(self) -> None:
        with patch.object(work_check, "t3_argv", return_value=None):
            assert _REAL_FETCH_ORPHANS() is None

    def test_a_json_list_is_returned(self) -> None:
        with _fake_t3(returncode=0, stdout='[{"branch": "b"}]'):
            assert _REAL_FETCH_ORPHANS() == [{"branch": "b"}]

    def test_a_failed_invocation_is_no_answer(self) -> None:
        with _fake_t3(returncode=1, stdout="[]"):
            assert _REAL_FETCH_ORPHANS() is None

    def test_unparseable_output_is_no_answer(self) -> None:
        with _fake_t3(returncode=0, stdout="not json"):
            assert _REAL_FETCH_ORPHANS() is None

    def test_a_non_list_payload_is_no_answer(self) -> None:
        with _fake_t3(returncode=0, stdout='{"branch": "b"}'):
            assert _REAL_FETCH_ORPHANS() is None

    def test_a_timing_out_invocation_is_no_answer(self) -> None:
        with (
            patch.object(work_check, "t3_argv", return_value=["/usr/bin/t3"]),
            patch.object(work_check, "run_t3", side_effect=subprocess.TimeoutExpired("t3", 4)),
        ):
            assert _REAL_FETCH_ORPHANS() is None

    def test_an_empty_list_is_an_answer(self) -> None:
        with _fake_t3(returncode=0, stdout="[]"):
            assert _REAL_FETCH_ORPHANS() == []
