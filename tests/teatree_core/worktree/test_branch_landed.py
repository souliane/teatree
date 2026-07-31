"""``branch_landed`` — has the branch's work already reached the base, and what would its PR do?

Real git under ``tmp_path``: the defect (#3977) is a path-level comparison missing content
that landed under a different path, so a mocked git call could not reproduce it — the blob
identity has to be git's own.
"""

from pathlib import Path

import pytest

from teatree.core.worktree.branch_landed import (
    REVERT_RISK_NET_REMOVED_LINES,
    assess_revert_risk,
    branch_content_landed_on_base,
)
from tests._git_repo import make_git_repo, run_git

FIX = "def parse(raw: str) -> int:\n    return int(raw.strip() or 0)\n"
TESTS = "def test_parse() -> None:\n    assert parse(' 7 ') == 7\n"


def _commit(repo: Path, message: str) -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


def _repo_with_branch_carrying_the_fix(tmp_path: Path) -> Path:
    """A repo whose ``main`` predates the fix and whose branch carries it plus its tests."""
    repo = make_git_repo(tmp_path / "clone")
    (repo / "app").mkdir()
    (repo / "app" / "parse.py").write_text("def parse(raw):\n    return 0\n")
    _commit(repo, "initial module")

    run_git(repo, "checkout", "-q", "-b", "fix/parse")
    (repo / "app" / "parse.py").write_text(FIX)
    (repo / "app" / "test_parse.py").write_text(TESTS)
    _commit(repo, "fix(parse): honour whitespace")
    run_git(repo, "checkout", "-q", "main")
    return repo


def _land_the_fix_under_a_different_path(repo: Path) -> None:
    """The same bytes reach ``main`` while the module is split, so every path moves."""
    (repo / "core").mkdir()
    (repo / "core" / "parsing.py").write_text(FIX)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_parsing.py").write_text(TESTS)
    (repo / "app" / "parse.py").unlink()
    _commit(repo, "refactor(core): split the parsing module")


class TestBranchContentLandedOnBase:
    def test_landed_when_the_same_bytes_reached_the_base_under_a_different_path(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        _land_the_fix_under_a_different_path(repo)

        assert branch_content_landed_on_base(str(repo), "fix/parse", "main") is True

    def test_not_landed_while_the_base_still_lacks_the_content(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        (repo / "README.md").write_text("unrelated base work\n")
        _commit(repo, "docs: unrelated")

        assert branch_content_landed_on_base(str(repo), "fix/parse", "main") is False

    def test_not_landed_when_only_part_of_the_branch_reached_the_base(self, tmp_path: Path) -> None:
        """The byte-identical tests landed; the module the branch changed did not."""
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        (repo / "tests").mkdir()
        (repo / "tests" / "test_parsing.py").write_text(TESTS)
        _commit(repo, "test(core): port the parsing tests")

        assert branch_content_landed_on_base(str(repo), "fix/parse", "main") is False

    def test_deletion_the_base_has_not_applied_still_owes(self, tmp_path: Path) -> None:
        """A branch whose whole contribution is a removal adds nothing the blob check can see."""
        repo = make_git_repo(tmp_path / "clone")
        (repo / "dead.py").write_text("obsolete = True\n")
        _commit(repo, "add the module")
        run_git(repo, "checkout", "-q", "-b", "chore/drop-dead")
        (repo / "dead.py").unlink()
        _commit(repo, "chore: drop the dead module")
        run_git(repo, "checkout", "-q", "main")

        assert branch_content_landed_on_base(str(repo), "chore/drop-dead", "main") is False

    def test_deletion_the_base_also_applied_is_landed(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "clone")
        (repo / "dead.py").write_text("obsolete = True\n")
        _commit(repo, "add the module")
        run_git(repo, "checkout", "-q", "-b", "chore/drop-dead")
        (repo / "dead.py").unlink()
        _commit(repo, "chore: drop the dead module")
        run_git(repo, "checkout", "-q", "main")
        (repo / "dead.py").unlink()
        _commit(repo, "chore: drop it on main instead")

        assert branch_content_landed_on_base(str(repo), "chore/drop-dead", "main") is True

    def test_a_rename_the_base_also_performed_is_landed(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "clone")
        (repo / "old.py").write_text(FIX)
        _commit(repo, "add the module")
        run_git(repo, "checkout", "-q", "-b", "chore/rename")
        run_git(repo, "mv", "old.py", "new.py")
        _commit(repo, "chore: rename the module")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "mv", "old.py", "new.py")
        _commit(repo, "chore: rename it on main too")

        assert branch_content_landed_on_base(str(repo), "chore/rename", "main") is True

    def test_a_branch_with_no_tree_delta_is_not_landed(self, tmp_path: Path) -> None:
        """Introducing nothing anywhere is not evidence of landing — the commits still need tracking."""
        repo = make_git_repo(tmp_path / "clone")
        run_git(repo, "checkout", "-q", "-b", "feat/empty")
        run_git(repo, "commit", "-q", "--allow-empty", "-m", "feat: a commit carrying no content")
        run_git(repo, "checkout", "-q", "main")

        assert branch_content_landed_on_base(str(repo), "feat/empty", "main") is False

    @pytest.mark.parametrize(("branch", "target"), [("no/such/branch", "main"), ("fix/parse", "origin/nope")])
    def test_unresolvable_ref_fails_closed(self, tmp_path: Path, branch: str, target: str) -> None:
        """An obligation is never discharged on a probe that could not run — that loses work."""
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        assert branch_content_landed_on_base(str(repo), branch, target) is False


class TestAssessRevertRisk:
    def test_names_the_base_content_a_pr_from_a_stale_branch_would_remove(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        (repo / "grown.py").write_text("".join(f"line {n}\n" for n in range(REVERT_RISK_NET_REMOVED_LINES + 50)))
        _commit(repo, "feat: a large refactor the branch predates")

        risk = assess_revert_risk(str(repo), "fix/parse", "main")

        assert risk.at_risk is True
        assert risk.net_removed >= REVERT_RISK_NET_REMOVED_LINES
        assert risk.files_changed >= 1

    def test_a_branch_that_only_adds_is_not_at_risk(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        risk = assess_revert_risk(str(repo), "fix/parse", "main")

        assert risk.at_risk is False
        assert risk.added > 0

    def test_a_binary_file_counts_as_a_file_not_as_lines(self, tmp_path: Path) -> None:
        """Git reports a binary row's counts as ``-``; parsing it as a number would crash."""
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        (repo / "logo.png").write_bytes(bytes(range(256)) * 8)
        _commit(repo, "feat: add a binary asset")

        risk = assess_revert_risk(str(repo), "fix/parse", "main")

        assert risk.measured is True
        assert risk.files_changed == 3
        assert risk.removed == 2

    def test_unresolvable_ref_reports_no_risk_rather_than_raising(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        risk = assess_revert_risk(str(repo), "no/such/branch", "main")

        assert risk.at_risk is False
        assert risk.measured is False
