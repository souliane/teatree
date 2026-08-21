"""``branch_landed`` — has the branch's work already reached the base, and what would its PR do?

Real git under ``tmp_path``: the defect (#3977) is a path-level comparison missing content
that landed under a different path, so a mocked git call could not reproduce it — the blob
identity has to be git's own.
"""

from pathlib import Path

import pytest

from teatree.core.worktree.branch_landed import (
    assess_revert_risk,
    branch_content_landed_on_base,
    pr_from_branch_would_be_empty,
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

    def test_a_deletion_is_not_landed_when_the_base_merely_rewrote_the_file(self, tmp_path: Path) -> None:
        """A base that REWROTE the path (not deleted it) has not discharged the removal.

        ``base.get(path) != blob`` is true both when the base deleted the path
        AND when it merely holds different content there — the file still
        EXISTS on the base, so the branch's deletion has not landed and would
        still conflict. Only absence from the base tree counts.
        """
        repo = make_git_repo(tmp_path / "clone")
        (repo / "f.py").write_text("obsolete = True\n")
        _commit(repo, "add f")
        run_git(repo, "checkout", "-q", "-b", "chore/drop-f")
        (repo / "f.py").unlink()
        _commit(repo, "chore: drop f")
        run_git(repo, "checkout", "-q", "main")
        (repo / "f.py").write_text("obsolete = False\n")
        _commit(repo, "rewrite f on main instead of dropping it")

        assert branch_content_landed_on_base(str(repo), "chore/drop-f", "main") is False

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

    def test_a_new_file_whose_content_only_pre_existed_elsewhere_is_not_landed(self, tmp_path: Path) -> None:
        """#3977 review: a byte-identical COPY of pre-existing base content is not "landed".

        The base never gains anything — the blob it always held stays exactly
        where it always was. Matching on raw presence alone (not NEW occurrences)
        would falsely discharge a branch whose real contribution never reached
        the base, dropping it from tracking entirely.
        """
        repo = make_git_repo(tmp_path / "clone")
        (repo / "config").mkdir()
        (repo / "config" / "prod.yml").write_text("replicas: 3\ntimeout: 30\n")
        _commit(repo, "base config")
        run_git(repo, "checkout", "-q", "-b", "feat/staging-env")
        (repo / "config" / "staging.yml").write_text("replicas: 3\ntimeout: 30\n")
        _commit(repo, "feat: add a staging env config")
        run_git(repo, "checkout", "-q", "main")

        assert branch_content_landed_on_base(str(repo), "feat/staging-env", "main") is False

    def test_an_empty_file_whose_blob_already_existed_is_not_landed(self, tmp_path: Path) -> None:
        """The empty blob exists in nearly every repo — presence alone proves nothing."""
        repo = make_git_repo(tmp_path / "clone")
        (repo / "pkg_a").mkdir()
        (repo / "pkg_a" / "__init__.py").write_text("")
        (repo / "pkg_a" / "m.py").write_text("x = 1\n")
        _commit(repo, "base pkg")
        run_git(repo, "checkout", "-q", "-b", "fix/packaging")
        (repo / "pkg_b").mkdir()
        (repo / "pkg_b" / "__init__.py").write_text("")
        _commit(repo, "fix: make pkg_b a package")
        run_git(repo, "checkout", "-q", "main")

        assert branch_content_landed_on_base(str(repo), "fix/packaging", "main") is False

    @pytest.mark.parametrize(("branch", "target"), [("no/such/branch", "main"), ("fix/parse", "origin/nope")])
    def test_unresolvable_ref_fails_closed(self, tmp_path: Path, branch: str, target: str) -> None:
        """An obligation is never discharged on a probe that could not run — that loses work."""
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        assert branch_content_landed_on_base(str(repo), branch, target) is False


class TestPrFromBranchWouldBeEmpty:
    """#4429: the branch that squash-merged, then merged the base back in."""

    def _base_takes_the_fix_and_the_branch_merges_it_back(self, tmp_path: Path) -> Path:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        (repo / "app" / "parse.py").write_text(FIX)
        (repo / "app" / "test_parse.py").write_text(TESTS)
        _commit(repo, "fix(parse): honour whitespace (#4422)")
        run_git(repo, "checkout", "-q", "fix/parse")
        run_git(repo, "merge", "-q", "--no-edit", "main")
        return repo

    def test_empty_once_the_base_holds_the_work_the_branch_merged_back(self, tmp_path: Path) -> None:
        repo = self._base_takes_the_fix_and_the_branch_merges_it_back(tmp_path)

        assert pr_from_branch_would_be_empty(str(repo), "fix/parse", "main") is True

    def test_empty_even_when_the_base_moved_on_afterwards(self, tmp_path: Path) -> None:
        """The forge diffs from the merge base, so later base work is not the branch's delta."""
        repo = self._base_takes_the_fix_and_the_branch_merges_it_back(tmp_path)
        run_git(repo, "checkout", "-q", "main")
        (repo / "CHANGELOG.md").write_text("the base moves on\n")
        _commit(repo, "docs: unrelated base work")

        assert pr_from_branch_would_be_empty(str(repo), "fix/parse", "main") is True

    def test_not_empty_while_the_branch_still_carries_work(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        assert pr_from_branch_would_be_empty(str(repo), "fix/parse", "main") is False

    @pytest.mark.parametrize(("branch", "target"), [("no/such/branch", "main"), ("fix/parse", "origin/nope")])
    def test_unresolvable_ref_fails_closed(self, tmp_path: Path, branch: str, target: str) -> None:
        """A probe that could not run never suppresses a pull request the branch owes."""
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        assert pr_from_branch_would_be_empty(str(repo), branch, target) is False


class TestAssessRevertRisk:
    def test_a_branch_merely_behind_an_active_base_is_not_at_risk(self, tmp_path: Path) -> None:
        """#3977 review: a real merge is unaffected by base's UNRELATED progress.

        A two-dot line-count would falsely flag this branch, because base grew
        substantially — but nothing base did touches anything the branch
        touched, so a real merge is clean.
        """
        repo = _repo_with_branch_carrying_the_fix(tmp_path)
        (repo / "unrelated.py").write_text("".join(f"line {n}\n" for n in range(300)))
        _commit(repo, "feat: ordinary churn on an active base")

        risk = assess_revert_risk(str(repo), "fix/parse", "main")

        assert risk.measured is True
        assert risk.at_risk is False
        assert risk.conflicted_paths == ()

    def test_a_branch_that_only_adds_is_not_at_risk(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        risk = assess_revert_risk(str(repo), "fix/parse", "main")

        assert risk.at_risk is False
        assert risk.measured is True

    def test_a_branch_modifying_a_file_the_base_independently_deletes_is_at_risk(self, tmp_path: Path) -> None:
        """The canonical revert shape: base's refactor conflicts with the branch's own edit."""
        repo = make_git_repo(tmp_path / "clone")
        (repo / "app").mkdir()
        (repo / "app" / "parse.py").write_text("def parse(x):\n    return 0\n")
        _commit(repo, "base")
        run_git(repo, "checkout", "-q", "-b", "fix/parse2")
        (repo / "app" / "parse.py").write_text("def parse(x):\n    return int(x)\n")
        _commit(repo, "fix: real fix, base unaware")
        run_git(repo, "checkout", "-q", "main")
        (repo / "app" / "parse.py").unlink()
        _commit(repo, "refactor: remove the module")

        risk = assess_revert_risk(str(repo), "fix/parse2", "main")

        assert risk.measured is True
        assert risk.at_risk is True
        assert risk.conflicted_paths == ("app/parse.py",)

    def test_unresolvable_ref_reports_no_risk_rather_than_raising(self, tmp_path: Path) -> None:
        repo = _repo_with_branch_carrying_the_fix(tmp_path)

        risk = assess_revert_risk(str(repo), "no/such/branch", "main")

        assert risk.at_risk is False
        assert risk.measured is False

    def test_unrelated_histories_report_no_risk_rather_than_raising(self, tmp_path: Path) -> None:
        """A merge simulation that fails for a reason other than "conflicts" fails closed too."""
        repo = make_git_repo(tmp_path / "clone")
        run_git(repo, "checkout", "-q", "--orphan", "unrelated")
        run_git(repo, "commit", "-q", "--allow-empty", "-m", "feat: a root commit sharing no history")

        risk = assess_revert_risk(str(repo), "unrelated", "main")

        assert risk.at_risk is False
        assert risk.measured is False
