"""The branch-upstream invariant (souliane/teatree#4225).

Exercised against real ``git`` under ``tmp_path``: the whole subject is what git
writes into ``branch.<n>.merge`` for a given ``worktree add`` form, which a mock
would simply assert back at itself. Each conformance test is paired with the
control that produces the defect, so a green is evidence rather than a shape.
"""

from pathlib import Path
from unittest import mock

import pytest

from teatree.utils.git_run import check as real_check
from teatree.utils.git_upstream import (
    BranchUpstream,
    branch_upstream,
    mistracked_branches,
    normalize_branch_upstream,
    repair_mistracked_branches,
)
from teatree.utils.run import run_checked


def _git(cwd: Path, *args: str) -> str:
    return run_checked(
        ["git", "-c", "user.email=agent@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
    ).stdout.strip()


def _merge_ref(repo: Path, branch: str) -> str:
    return branch_upstream(str(repo), branch).merge_ref


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of a bare origin, on ``main``, with one pushed commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(root))
    (root / "file.txt").write_text("hello", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    _git(root, "push", "origin", "main")
    return root


class TestClassification:
    def test_a_branch_cut_from_origin_main_is_mistracked(self, clone: Path, tmp_path: Path) -> None:
        # The control for every conformance assertion below: this is the exact
        # published recipe, and it is what put refs/heads/main on 20 branches.
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")

        assert _merge_ref(clone, "feat") == "refs/heads/main"
        assert [entry.branch for entry in mistracked_branches(str(clone))] == ["feat"]

    def test_no_track_on_the_same_recipe_is_conformant(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", "--no-track", str(tmp_path / "wt"), "origin/main")

        assert _merge_ref(clone, "feat") == ""
        assert mistracked_branches(str(clone)) == []

    def test_a_branch_tracking_its_own_ref_is_conformant(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"))
        _git(clone, "push", "-u", "origin", "feat")

        assert _merge_ref(clone, "feat") == "refs/heads/feat"
        assert mistracked_branches(str(clone)) == []

    def test_tracking_another_feature_branch_is_mistracked(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "base", str(tmp_path / "base"))
        _git(clone, "push", "-u", "origin", "base")
        _git(clone, "worktree", "add", "-b", "stacked", str(tmp_path / "stacked"), "origin/base")

        assert [entry.merge_ref for entry in mistracked_branches(str(clone))] == ["refs/heads/base"]

    def test_a_dotted_branch_name_survives_the_config_key_split(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "fix/v1.2.merge", str(tmp_path / "wt"), "origin/main")

        assert [entry.branch for entry in mistracked_branches(str(clone))] == ["fix/v1.2.merge"]

    def test_main_itself_is_conformant(self, clone: Path) -> None:
        assert _merge_ref(clone, "main") == "refs/heads/main"
        assert mistracked_branches(str(clone)) == []

    def test_an_empty_branch_name_in_the_config_key_is_ignored(self, clone: Path) -> None:
        # git's config CLI accepts `branch..merge` (empty subsection) verbatim,
        # and it matches the parser's regex — the split must not surface it as
        # a branch named "".
        _git(clone, "config", "branch..merge", "refs/heads/x")

        assert mistracked_branches(str(clone)) == []


class TestRemedy:
    def test_names_unset_when_the_branch_has_no_remote_of_its_own(self) -> None:
        entry = BranchUpstream(branch="feat", remote="origin", merge_ref="refs/heads/main", own_remote_ref_exists=False)

        assert entry.remedy == "git branch --unset-upstream feat"

    def test_names_set_upstream_when_the_branch_was_pushed(self) -> None:
        entry = BranchUpstream(branch="feat", remote="origin", merge_ref="refs/heads/main", own_remote_ref_exists=True)

        assert entry.remedy == "git branch --set-upstream-to=origin/feat feat"

    def test_a_blank_remote_falls_back_to_origin(self) -> None:
        entry = BranchUpstream(branch="feat", remote="", merge_ref="refs/heads/main", own_remote_ref_exists=True)

        assert entry.effective_remote == "origin"

    def test_local_only_tracking_falls_back_to_origin(self) -> None:
        entry = BranchUpstream(branch="feat", remote=".", merge_ref="refs/heads/main", own_remote_ref_exists=True)

        assert entry.effective_remote == "origin"


class TestNormalize:
    def test_unpushed_branch_has_its_stale_tracking_removed(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")

        outcome = normalize_branch_upstream(str(clone), "feat")

        assert "refs/heads/main -> unset" in outcome
        assert _merge_ref(clone, "feat") == ""

    def test_the_repair_never_renders_an_unpushed_branch_gone(self, clone: Path, tmp_path: Path) -> None:
        # `[gone]` is what prune_branches force-deletes on, so repairing an
        # unpushed branch TO its own absent ref would make clean-all destroy it.
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")

        normalize_branch_upstream(str(clone), "feat")

        assert "[gone]" not in _git(clone, "branch", "-v", "--no-color")

    def test_the_repair_never_attempts_set_upstream_to_on_an_absent_ref(self, clone: Path, tmp_path: Path) -> None:
        # Git itself REFUSES `--set-upstream-to=origin/<absent-ref>` (measured), so
        # the `[gone]`-state assertion above stays green even on a mutant that
        # ALWAYS attempts it — git's refusal masks the mutant, not the
        # implementation. Pin the CALL instead: an unpushed branch's repair must
        # never emit `--set-upstream-to`.
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")
        calls: list[list[str]] = []

        def spy(*, repo: str, args: list[str]) -> bool:
            calls.append(args)
            return real_check(repo=repo, args=args)

        with mock.patch("teatree.utils.git_upstream.check", side_effect=spy):
            normalize_branch_upstream(str(clone), "feat")

        assert calls == [["branch", "--unset-upstream", "feat"]]

    def test_pushed_branch_is_repointed_at_its_own_ref(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")
        _git(clone, "push", "origin", "feat")

        outcome = normalize_branch_upstream(str(clone), "feat")

        assert outcome == "Repaired feat: refs/heads/main -> refs/heads/feat"
        assert _merge_ref(clone, "feat") == "refs/heads/feat"

    def test_a_conformant_branch_is_left_untouched(self, clone: Path) -> None:
        assert normalize_branch_upstream(str(clone), "main") == ""
        assert _merge_ref(clone, "main") == "refs/heads/main"

    def test_a_write_that_did_not_land_reports_failure_with_the_manual_remedy(
        self, clone: Path, tmp_path: Path
    ) -> None:
        # The verify-by-re-read contract: a git config write can exit 0 without
        # applying, and the exit code alone would report that as a repair.
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")

        with mock.patch("teatree.utils.git_upstream.check", return_value=True) as did_not_write:
            outcome = normalize_branch_upstream(str(clone), "feat")

        assert did_not_write.called
        assert outcome == "FAILED feat: upstream is still refs/heads/main — run: git branch --unset-upstream feat"

    def test_an_untracked_branch_is_left_untracked(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", "--no-track", str(tmp_path / "wt"), "origin/main")

        assert normalize_branch_upstream(str(clone), "feat") == ""
        assert _merge_ref(clone, "feat") == ""


class TestRepairSweep:
    def test_repairs_every_mistracked_branch_and_is_idempotent(self, clone: Path, tmp_path: Path) -> None:
        for name in ("one", "two"):
            _git(clone, "worktree", "add", "-b", name, str(tmp_path / name), "origin/main")

        first = repair_mistracked_branches(str(clone))

        assert [line.split(":")[0] for line in first] == ["Repaired one", "Repaired two"]
        assert mistracked_branches(str(clone)) == []
        assert repair_mistracked_branches(str(clone)) == []

    def test_dry_run_reports_without_writing(self, clone: Path, tmp_path: Path) -> None:
        _git(clone, "worktree", "add", "-b", "feat", str(tmp_path / "wt"), "origin/main")

        planned = repair_mistracked_branches(str(clone), dry_run=True)

        assert planned == ["Would repair feat (refs/heads/main): git branch --unset-upstream feat"]
        assert _merge_ref(clone, "feat") == "refs/heads/main"
