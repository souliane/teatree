"""The clone-wide branch-upstream sweep (souliane/teatree#4225).

Functional: real clones under ``tmp_path``, reached through the same
``known_clone_paths`` enumeration the other clone-wide passes use, so the sweep
is exercised over that seam rather than over a hand-passed path list.
"""

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from teatree.core.worktree.branch_upstream import repair_clones, scan_clones
from teatree.utils.git_upstream import branch_upstream
from teatree.utils.run import run_checked


def _git(cwd: Path, *args: str) -> str:
    return run_checked(
        ["git", "-c", "user.email=agent@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Iterator[Path]:
    """The only known clone, holding a ``feat`` branch cut the way the recipe cut 20 of them."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(root))
    (root / "file.txt").write_text("hello", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    _git(root, "push", "origin", "main")
    _git(root, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")
    with mock.patch("teatree.core.worktree.branch_upstream.known_clone_paths", return_value={root}):
        yield root


class TestScanClones:
    def test_reports_the_mistracked_branch_with_its_remedy(self, clone: Path) -> None:
        [found] = scan_clones()

        assert [entry.branch for entry in found.branches] == ["feat"]
        assert found.findings() == [
            f"{clone}: branch 'feat' tracks refs/heads/main — Fix: git -C {clone} branch --unset-upstream feat"
        ]

    def test_a_conformant_clone_yields_nothing(self, clone: Path) -> None:
        _git(clone, "branch", "--unset-upstream", "feat")

        assert scan_clones() == []


class TestRepairClones:
    def test_clears_the_finding_and_is_idempotent(self, clone: Path) -> None:
        repaired = repair_clones()

        assert repaired == [f"{clone}: Repaired feat: refs/heads/main -> unset (no remote branch to track)"]
        assert scan_clones() == []
        assert repair_clones() == []

    def test_dry_run_reports_without_writing(self, clone: Path) -> None:
        planned = repair_clones(dry_run=True)

        assert planned == [f"{clone}: Would repair feat (refs/heads/main): git branch --unset-upstream feat"]
        assert branch_upstream(str(clone), "feat").merge_ref == "refs/heads/main"
