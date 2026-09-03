"""Session-end unshipped-work backstop — armed unconditionally, covering all five states.

The defect this pins: the backstop only ran when a lifecycle skill happened to be
loaded, and it only ever looked at orphan branches. Whether a session stranded work
is not a function of which skills it loaded.
"""

import contextlib
import json
import shutil
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.session_end_work_check as work_check
from hooks.scripts.hook_router import handle_session_end


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


@pytest.fixture(autouse=True)
def _no_real_probes():
    """Never shell out to the real t3 / gh from a unit test."""
    with (
        patch.object(work_check, "fetch_orphans", return_value=[]),
        patch.object(work_check, "open_prs_for_repo", return_value=[]),
    ):
        yield


def _run(data: dict) -> str:
    stdout = StringIO()
    with patch("sys.stdout", stdout):
        handle_session_end(data)
    return stdout.getvalue()


def _context(data: dict) -> str:
    raw = _run(data)
    return json.loads(raw)["additionalContext"] if raw else ""


_GIT = shutil.which("git") or "git"
#: Captured before the autouse fixture patches it, so this module can exercise the real probe.
_REAL_FETCH_ORPHANS = work_check.fetch_orphans


def _git(repo: Path, *args: str) -> None:
    env = {"HOME": str(repo.parent), "PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run([_GIT, "-C", str(repo), *args], check=True, capture_output=True, env=env)


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


def _repo_with_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work-branch")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


class TestArmedUnconditionally:
    """The check must not be gated on which skills the session happened to load."""

    def test_orphan_reported_with_no_lifecycle_skill_loaded(self) -> None:
        orphans = [{"repo": "/ws/backend", "branch": "feat-1", "status": "pushed_orphan", "ahead_count": 3}]
        with patch.object(work_check, "fetch_orphans", return_value=orphans):
            ctx = _context({"session_id": "s-no-skills"})

        assert "feat-1" in ctx
        assert "/ws/backend" in ctx
        assert "ensure-pr" in ctx

    def test_orphan_reported_when_only_non_lifecycle_skills_loaded(self) -> None:
        (router.STATE_DIR / "s-other.skills").write_text("ac-python\n", encoding="utf-8")
        orphans = [{"repo": "/ws/backend", "branch": "feat-2", "status": "pushed_orphan", "ahead_count": 1}]
        with patch.object(work_check, "fetch_orphans", return_value=orphans):
            ctx = _context({"session_id": "s-other"})

        assert "feat-2" in ctx

    def test_silent_when_nothing_is_stranded(self) -> None:
        assert _run({"session_id": "s-clean"}) == ""

    def test_silent_without_a_session_id(self) -> None:
        assert _run({"session_id": ""}) == ""

    def test_retro_suggestion_still_fires_on_lifecycle_skills(self) -> None:
        (router.STATE_DIR / "s-retro.skills").write_text("t3:code\nac-python\n", encoding="utf-8")
        ctx = _context({"session_id": "s-retro"})

        assert "retro" in ctx.lower()
        assert "t3:code" in ctx
        assert "ac-python" not in ctx
        assert "UNSHIPPED WORK" not in ctx

    def test_long_orphan_list_is_previewed(self) -> None:
        many = [
            {"repo": f"/ws/r{i}", "branch": f"br-{i}", "status": "pushed_orphan", "ahead_count": 1} for i in range(8)
        ]
        with patch.object(work_check, "fetch_orphans", return_value=many):
            ctx = _context({"session_id": "s-many"})

        assert ctx.count("[orphan_branch]") == work_check.PREVIEW_LIMIT


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

    def test_clean_synced_repo_reports_nothing(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        with patch.object(work_check, "unpushed_commit_count", return_value=0):
            assert _run({"session_id": "s-clean-repo", "cwd": str(repo)}) == ""


class TestOpenPullRequest:
    def test_open_pr_authored_by_this_session_is_reported(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        prs = [{"number": 42, "title": "Add the thing", "headRefName": "work-branch"}]
        with (
            patch.object(work_check, "open_prs_for_repo", return_value=prs),
            patch.object(work_check, "unpushed_commit_count", return_value=0),
        ):
            ctx = _context({"session_id": "s-pr", "cwd": str(repo)})

        assert "#42" in ctx
        assert "Add the thing" in ctx
        assert "loops tick --loop ship" in ctx


class TestCrashProof:
    def test_a_raising_probe_never_breaks_the_session(self) -> None:
        with patch.object(work_check, "fetch_orphans", side_effect=RuntimeError("boom")):
            assert _run({"session_id": "s-boom"}) == ""

    def test_a_nonexistent_cwd_is_ignored(self) -> None:
        assert _run({"session_id": "s-nodir", "cwd": "/definitely/not/a/dir"}) == ""


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

    def test_a_synced_upstream_counts_zero(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        _upstream(repo, tmp_path / "origin.git")

        assert work_check.unpushed_commit_count(repo) == 0


class TestFetchOrphans:
    def test_a_missing_t3_binary_yields_nothing(self) -> None:
        with patch.object(work_check, "t3_argv", return_value=None):
            assert _REAL_FETCH_ORPHANS() == []

    def test_a_json_list_is_returned(self) -> None:
        with _fake_t3(returncode=0, stdout='[{"branch": "b"}]'):
            assert _REAL_FETCH_ORPHANS() == [{"branch": "b"}]

    def test_a_failed_invocation_yields_nothing(self) -> None:
        with _fake_t3(returncode=1, stdout="[]"):
            assert _REAL_FETCH_ORPHANS() == []

    def test_unparseable_output_yields_nothing(self) -> None:
        with _fake_t3(returncode=0, stdout="not json"):
            assert _REAL_FETCH_ORPHANS() == []

    def test_a_non_list_payload_yields_nothing(self) -> None:
        with _fake_t3(returncode=0, stdout='{"branch": "b"}'):
            assert _REAL_FETCH_ORPHANS() == []

    def test_a_timing_out_invocation_yields_nothing(self) -> None:
        with (
            patch.object(work_check, "t3_argv", return_value=["/usr/bin/t3"]),
            patch.object(work_check, "run_t3", side_effect=subprocess.TimeoutExpired("t3", 4)),
        ):
            assert _REAL_FETCH_ORPHANS() == []
