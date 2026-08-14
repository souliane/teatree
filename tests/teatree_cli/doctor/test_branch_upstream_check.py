"""The ``check_branch_upstreams`` doctor probe (souliane/teatree#4225).

Functional: a real clone under ``tmp_path``, so the FAIL text is produced from
git's own config rather than from a stubbed finding.
"""

import io
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from teatree.cli.doctor.checks_branch_upstream import check_branch_upstreams
from teatree.utils.run import run_checked


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _git(cwd: Path, *args: str) -> str:
    return run_checked(
        ["git", "-c", "user.email=agent@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Iterator[Path]:
    """The only known clone, conformant until a test cuts a branch off ``origin/main``."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(root))
    (root / "file.txt").write_text("hello", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    _git(root, "push", "origin", "main")
    with mock.patch("teatree.core.worktree.branch_upstream.known_clone_paths", return_value={root}):
        yield root


class TestCheckBranchUpstreams:
    def test_passes_silently_when_every_branch_tracks_its_own_ref(self, clone: Path) -> None:
        assert clone.is_dir()

        ok, out = _echoes(check_branch_upstreams)

        assert ok is True
        assert out == ""

    def test_fails_and_names_the_branch_the_ref_and_the_remedy(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")

        ok, out = _echoes(check_branch_upstreams)

        assert ok is False
        assert "FAIL  1 branch(es) track an upstream that is not their own" in out
        assert "branch 'feat' tracks refs/heads/main" in out
        assert f"git -C {clone} branch --unset-upstream feat" in out
        assert "t3 <overlay> workspace repair-branch-upstreams" in out

    def test_an_unreadable_clone_set_warns_rather_than_failing(self) -> None:
        with mock.patch(
            "teatree.core.worktree.branch_upstream.known_clone_paths",
            side_effect=RuntimeError("no database"),
        ):
            ok, out = _echoes(check_branch_upstreams)

        assert ok is True
        assert "WARN  Branch upstreams UNVERIFIED" in out
