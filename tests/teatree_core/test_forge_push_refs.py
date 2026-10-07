"""Integration tests for the one ref form ``t3 push`` uses (souliane/teatree#4117).

Real git repos under ``tmp_path``: a tag shadowing a branch is the only condition
under which the bare and qualified spellings disagree, so it is the condition every
case here is built on.
"""

from pathlib import Path

import pytest

from teatree.core.forge_push_refs import BranchRef, local_tip, operation_in_progress
from tests._git_repo import make_git_repo, run_git


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone on `feature`, with a `feature` TAG at the commit BEFORE the branch tip."""
    repo = make_git_repo(tmp_path / "clone")
    run_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("work\n")
    run_git(repo, "add", "file.txt")
    run_git(repo, "commit", "-q", "-m", "work")
    run_git(repo, "tag", "feature", "HEAD")
    (repo / "more.txt").write_text("more\n")
    run_git(repo, "add", "more.txt")
    run_git(repo, "commit", "-q", "-m", "more")
    return repo


class TestBranchRefResolvesToOneSpelling:
    @pytest.mark.parametrize("spelling", ["", "HEAD", "feature", "refs/heads/feature"])
    def test_every_spelling_git_push_accepts_names_the_same_branch(self, clone: Path, spelling: str) -> None:
        resolved = BranchRef.resolve(repo=str(clone), branch=spelling)

        assert resolved == BranchRef(name="feature")
        assert resolved.qualified == "refs/heads/feature"

    def test_a_detached_head_resolves_to_no_branch(self, clone: Path) -> None:
        run_git(clone, "checkout", "-q", "--detach", "HEAD")

        assert BranchRef.resolve(repo=str(clone), branch="").name == ""

    @pytest.mark.parametrize("spelling", ["HEAD:topic", "HEAD:refs/heads/topic"])
    def test_head_colon_a_branch_publishes_head_under_that_branch(self, clone: Path, spelling: str) -> None:
        resolved = BranchRef.resolve(repo=str(clone), branch=spelling)

        assert resolved == BranchRef(name="topic", from_head=True)
        assert resolved.refspec == "HEAD:refs/heads/topic"

    def test_a_plain_branch_keeps_its_single_ref_refspec(self, clone: Path) -> None:
        assert BranchRef.resolve(repo=str(clone), branch="feature").refspec == "refs/heads/feature"

    @pytest.mark.parametrize(
        "spelling",
        [":feature", "+HEAD:feature", "+feature", "other:feature", "HEAD:", "HEAD:HEAD", "HEAD:refs/tags/v1"],
    )
    def test_a_delete_force_rename_or_non_branch_refspec_is_unsupported(self, clone: Path, spelling: str) -> None:
        resolved = BranchRef.resolve(repo=str(clone), branch=spelling)

        assert resolved.unsupported == spelling
        assert not resolved.name


class TestLocalTipReadsTheReturnCode:
    """`git rev-parse` echoes an unresolvable argument back — rc is the only honest signal."""

    def test_an_unresolvable_ref_yields_no_sha_rather_than_its_own_name(self, clone: Path) -> None:
        assert local_tip(repo=str(clone), ref="refs/heads/no-such-branch") == ""

    def test_a_qualified_branch_ref_yields_the_branch_sha_never_the_tags(self, clone: Path) -> None:
        assert local_tip(repo=str(clone), ref="refs/heads/feature") == run_git(clone, "rev-parse", "HEAD")
        assert local_tip(repo=str(clone), ref="refs/heads/feature") != run_git(clone, "rev-parse", "refs/tags/feature")


class TestOperationInProgressFailsClosed:
    def test_a_state_git_cannot_report_counts_as_an_operation(self, tmp_path: Path) -> None:
        assert operation_in_progress(str(tmp_path))
