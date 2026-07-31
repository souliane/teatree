"""A checkout whose only work is UNCOMMITTED must not hand the skill a redundant record (#3822).

``clean-all`` keeps a dirty worktree and prints "salvage, do not wipe" — but the
machine record it emits alongside describes only COMMITS. A worktree whose entire
delta is staged-but-uncommitted has no commits ahead, so the record carried
``unique_commit_shas: []`` with ``content_verified: true``, which the judgment
skill reads as proven-redundant and routes to DELETE. The CLI's own verdict and
the record it emits disagreed, and the record is the one a machine acts on.

These pin the three links: the working-tree probe reports staged paths, the emit
record carries them, and a record naming them can never serialise as verified.
"""

from pathlib import Path
from typing import cast
from unittest.mock import patch

from teatree.core.cleanup.cleanup import _EffectiveTarget
from teatree.core.cleanup.cleanup_emit import EMIT_SCHEMA_VERSION, CleanupEmitRecord
from teatree.core.cleanup.working_tree_dirt import WorkingTreeDirt, working_tree_dirt
from teatree.core.models import Worktree
from teatree.core.worktree import worktree_done as worktree_done_mod
from tests.teatree_core.cleanup._shared import _run_git


def _staged_only_worktree(tmp_path: Path) -> tuple[Path, _EffectiveTarget]:
    """A worktree holding one STAGED new file and nothing committed beyond the base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=repo)
    _run_git("config", "user.email", "t@t", cwd=repo)
    _run_git("config", "user.name", "t", cwd=repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _run_git("add", "-A", cwd=repo)
    _run_git("commit", "-q", "-m", "initial", cwd=repo)

    wt_dir = tmp_path / "wt"
    _run_git("worktree", "add", "-q", "-b", "feat", str(wt_dir), cwd=repo)
    (wt_dir / "gate.py").write_text("GATE = True\n", encoding="utf-8")
    _run_git("add", "gate.py", cwd=wt_dir)
    target = _EffectiveTarget(ref="HEAD", probe_repo=str(wt_dir), branch_to_delete="feat", label="feat")
    return wt_dir, target


class TestWorkingTreeDirtNamesTheStagedPaths:
    def test_staged_only_file_is_reported_as_a_path(self, tmp_path: Path) -> None:
        """The probe already KNOWS the paths; they must be readable, not collapsed to prose."""
        wt_dir, target = _staged_only_worktree(tmp_path)
        dirt = working_tree_dirt(str(wt_dir), target)
        assert dirt.proven is True
        assert dirt.paths == ("gate.py",)

    def test_clean_worktree_has_no_paths(self, tmp_path: Path) -> None:
        wt_dir, target = _staged_only_worktree(tmp_path)
        _run_git("commit", "-q", "-m", "gate", cwd=wt_dir)
        assert working_tree_dirt(str(wt_dir), target).paths == ()


class TestEmitRecordNeverContradictsItself:
    def test_uncommitted_paths_force_the_record_unverified(self) -> None:
        record = CleanupEmitRecord(
            path="/ws/feat",
            branch="feat",
            kind="worktree",
            unique_commit_shas=[],
            content_verified=True,
            verdict_source="cherry-zero-unique",
            uncommitted_paths=["src/teatree/core/models/phase_coverage_gate.py"],
        )
        data = record.to_dict()
        assert data["uncommitted_paths"] == ["src/teatree/core/models/phase_coverage_gate.py"]
        assert data["content_verified"] is False, "work on no ref must never read as proven-redundant"
        assert data["verdict_source"] == "uncommitted-work"
        assert data["schema_version"] == EMIT_SCHEMA_VERSION

    def test_clean_record_is_unchanged(self) -> None:
        data = CleanupEmitRecord(
            path="/ws/feat",
            branch="feat",
            kind="worktree",
            content_verified=True,
            verdict_source="cherry-zero-unique",
        ).to_dict()
        assert data["uncommitted_paths"] == []
        assert data["content_verified"] is True
        assert data["verdict_source"] == "cherry-zero-unique"


class TestBuilderWiresTheDirtProbeIntoTheRecord:
    def test_build_emit_record_carries_the_uncommitted_paths(self) -> None:
        """The builder must consult the working tree, not only the commit graph."""
        worktree = Worktree(repo_path="teatree", branch="feat", extra={"worktree_path": "/ws/feat"})
        dirt = WorkingTreeDirt(reasons=("1 uncommitted change(s): gate.py",), proven=True, paths=("gate.py",))
        with (
            patch.object(worktree_done_mod, "working_tree_dirt", return_value=dirt),
            patch.object(worktree_done_mod.git, "run", return_value=""),
            patch.object(worktree_done_mod.git, "run_strict", return_value=""),
        ):
            record = worktree_done_mod._build_emit_record(worktree, workspace=Path("/ws"), liveness="")
        assert record.uncommitted_paths == ["gate.py"]
        assert cast("dict", record.to_dict())["content_verified"] is False
