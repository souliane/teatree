"""Pre-cold-review / pre-ship branch-currency auto-merge gate (#940).

The exit-point sibling of :mod:`teatree.core.gates.clone_guard` (#948 — the
entry-point pre-investigation gate). Before a cold reviewer attests a
PR's SHA, and before ``ship`` pushes a stale base into review, this gate
re-fetches the target branch and auto-merges it into the feature branch
on a zero-conflict fast-forward/merge so the cold reviewer attests the
**post-merge** tree. On conflict it refuses cleanly (``git merge
--abort``) — never leaves the worktree half-merged.

Real ``git init`` under ``tmp_path`` exercises three scenarios: a
zero-conflict auto-merge, a conflicting overlap that aborts cleanly,
and an already-current no-op. Each asserts the
:class:`BranchCurrencyResult` fields (the public contract), not
mock-call counts.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.core import branch_currency as branch_currency_module
from teatree.core.branch_currency import (
    BranchStaleness,
    MergeConflict,
    MergeOutcome,
    auto_merge_target,
    branch_behind_target,
    require_current_branch,
    sha_conflicts_with_target,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_remote(tmp_path: Path) -> Path:
    """Create a bare remote with one commit on ``main`` containing ``a.txt``."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(seed, "config", "user.name", "Tester")
    (seed / "a.txt").write_text("base\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "initial")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _clone(tmp_path: Path, bare: Path, name: str = "clone") -> Path:
    clone = tmp_path / name
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    return clone


def _advance_remote(tmp_path: Path, bare: Path, *, filename: str, content: str) -> None:
    """Add a new file (no overlap with feature work) on the remote's main."""
    work = tmp_path / f"advance-{filename}"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    (work / filename).write_text(content)
    _git(work, "add", filename)
    _git(work, "commit", "-m", f"remote: add {filename}")
    _git(work, "push", "origin", "main")


def _advance_remote_overlap(tmp_path: Path, bare: Path) -> None:
    """Advance the remote with a change to ``a.txt`` that will conflict."""
    work = tmp_path / "advance-overlap"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    (work / "a.txt").write_text("remote-change\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "remote: change a.txt")
    _git(work, "push", "origin", "main")


def _make_feature_branch(clone: Path, branch: str, filename: str, content: str) -> None:
    _git(clone, "checkout", "-b", branch)
    (clone / filename).write_text(content)
    _git(clone, "add", filename)
    _git(clone, "commit", "-m", f"feature: {filename}")


def _make_overlap_branch(clone: Path, branch: str) -> None:
    """Feature branch that touches ``a.txt`` (will conflict with remote-overlap)."""
    _git(clone, "checkout", "-b", branch)
    (clone / "a.txt").write_text("feature-change\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-m", "feature: change a.txt")


class TestBranchBehindTarget:
    def test_returns_none_when_branch_already_current(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        # No remote advance — branch is current with origin/main.
        result = branch_behind_target(str(clone), "feat/x")
        assert result is None

    def test_returns_staleness_when_target_advanced(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        _advance_remote(tmp_path, bare, filename="c.txt", content="remote-add\n")

        result = branch_behind_target(str(clone), "feat/x")

        assert isinstance(result, BranchStaleness)
        assert result.branch == "feat/x"
        assert result.target == "origin/main"
        assert result.behind_count == 1


class TestAutoMergeTarget:
    def test_zero_conflict_auto_merge_advances_branch(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        _advance_remote(tmp_path, bare, filename="c.txt", content="remote-add\n")
        # No overlap → must be a clean merge.
        pre_sha = _git(clone, "rev-parse", "HEAD")

        outcome = auto_merge_target(str(clone), "feat/x", "origin/main")

        assert outcome is MergeOutcome.ZERO_CONFLICT
        # Post-merge HEAD differs from pre-merge HEAD — the merge landed.
        post_sha = _git(clone, "rev-parse", "HEAD")
        assert post_sha != pre_sha
        # The remote's `c.txt` is now reachable from HEAD.
        assert (clone / "c.txt").exists()
        # No half-merged state.
        status = _git(clone, "status", "--porcelain")
        assert status == ""

    def test_conflict_aborts_and_leaves_clean_tree(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_overlap_branch(clone, "feat/x")
        _advance_remote_overlap(tmp_path, bare)
        pre_sha = _git(clone, "rev-parse", "HEAD")

        outcome = auto_merge_target(str(clone), "feat/x", "origin/main")

        assert outcome is MergeOutcome.CONFLICTED
        # HEAD did NOT move; worktree is clean (merge aborted).
        post_sha = _git(clone, "rev-parse", "HEAD")
        assert post_sha == pre_sha
        status = _git(clone, "status", "--porcelain")
        assert status == "", f"merge-abort left a dirty tree: {status!r}"

    def test_already_current_is_noop(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        # No remote advance.
        pre_sha = _git(clone, "rev-parse", "HEAD")

        outcome = auto_merge_target(str(clone), "feat/x", "origin/main")

        assert outcome is MergeOutcome.ALREADY_CURRENT
        assert _git(clone, "rev-parse", "HEAD") == pre_sha


class TestRequireCurrentBranch:
    def test_auto_merged_when_zero_conflict(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        _advance_remote(tmp_path, bare, filename="c.txt", content="remote-add\n")

        result = require_current_branch(str(clone), "feat/x", target="origin/main")

        assert isinstance(result, dict)
        assert result["auto_merged"] is True
        assert result["error"] is None
        post_merge_sha = result["post_merge_sha"]
        assert post_merge_sha is not None
        assert post_merge_sha == _git(clone, "rev-parse", "HEAD")

    def test_refuses_on_conflict_with_actionable_hint(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_overlap_branch(clone, "feat/x")
        _advance_remote_overlap(tmp_path, bare)

        result = require_current_branch(str(clone), "feat/x", target="origin/main")

        assert result["auto_merged"] is False
        error = result["error"]
        assert error is not None
        assert "conflict" in error.lower()
        assert "a.txt" in error
        # Hint must mention manual resolution path.
        hint = result["hint"]
        assert hint is not None
        assert "git merge" in hint

    def test_already_current_noop_returns_unchanged_sha(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        pre_sha = _git(clone, "rev-parse", "HEAD")

        result = require_current_branch(str(clone), "feat/x", target="origin/main")

        assert result["auto_merged"] is False
        assert result["error"] is None
        # Already-current: no merge performed, SHA unchanged.
        assert result["post_merge_sha"] is None or result["post_merge_sha"] == pre_sha

    def test_dry_run_does_not_merge(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        _advance_remote(tmp_path, bare, filename="c.txt", content="remote-add\n")
        pre_sha = _git(clone, "rev-parse", "HEAD")

        result = require_current_branch(str(clone), "feat/x", target="origin/main", dry_run=True)

        # No merge ran — HEAD unchanged.
        assert _git(clone, "rev-parse", "HEAD") == pre_sha
        # The result reports the staleness, no auto_merged side effect.
        assert result["auto_merged"] is False


class TestShaConflictsWithTarget:
    """Conflict-only CLEAR gate: behind alone is fine; only conflicts block."""

    def test_returns_none_when_already_current(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        # No remote advance — nothing behind, nothing to conflict.
        assert sha_conflicts_with_target(str(clone), "feat/x") is None

    def test_returns_none_when_behind_but_mergeable(self, tmp_path: Path) -> None:
        """The core requirement: a behind-but-conflict-free SHA is NOT blocked."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")
        # Target advances with a non-overlapping file — clean merge.
        _advance_remote(tmp_path, bare, filename="c.txt", content="remote-add\n")
        feature_sha = _git(clone, "rev-parse", "feat/x")

        # Confirm it really is behind (the old gate would have blocked here).
        assert branch_behind_target(str(clone), "feat/x") is not None
        # The conflict-only gate lets it through.
        assert sha_conflicts_with_target(str(clone), feature_sha) is None

    def test_returns_conflict_when_behind_and_overlapping(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_overlap_branch(clone, "feat/x")
        _advance_remote_overlap(tmp_path, bare)
        feature_sha = _git(clone, "rev-parse", "feat/x")

        result = sha_conflicts_with_target(str(clone), feature_sha)

        assert isinstance(result, MergeConflict)
        assert result.behind_count == 1
        assert "a.txt" in result.conflicting_paths

    def test_does_not_mutate_worktree(self, tmp_path: Path) -> None:
        """merge-tree prediction never touches HEAD or the working tree."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_overlap_branch(clone, "feat/x")
        _advance_remote_overlap(tmp_path, bare)
        pre_sha = _git(clone, "rev-parse", "HEAD")

        sha_conflicts_with_target(str(clone), "feat/x")

        assert _git(clone, "rev-parse", "HEAD") == pre_sha
        assert _git(clone, "status", "--porcelain") == ""

    def test_fetch_failure_is_inconclusive_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_overlap_branch(clone, "feat/x")
        _advance_remote_overlap(tmp_path, bare)
        monkeypatch.setattr(branch_currency_module, "_fetch_target", lambda repo, target: False)

        assert sha_conflicts_with_target(str(clone), "feat/x") is None


class TestFetchFailureSurface:
    def test_fetch_failure_surfaces_as_inconclusive_skip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed fetch must not block — same posture as clone_guard (#948)."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _make_feature_branch(clone, "feat/x", "b.txt", "feature\n")

        monkeypatch.setattr(branch_currency_module, "_fetch_target", lambda repo, target: False)

        result = require_current_branch(str(clone), "feat/x", target="origin/main")

        # Inconclusive — no error block, no merge.
        assert result["auto_merged"] is False
        assert result["error"] is None
