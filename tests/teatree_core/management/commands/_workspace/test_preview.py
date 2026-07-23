"""Each `clean-all` pass previews its own candidates under dry-run (souliane/teatree#3489).

The unit under test per case is the pass itself: same selection as a live run,
mutation skipped. A preview that under-reports what a destructive command will
do is worse than no preview, so each pass is asserted on BOTH halves — it names
the candidate AND it leaves the thing alone.
"""

from pathlib import Path
from unittest.mock import patch

from teatree.core.management.commands._workspace.cleanup import WorktreeReaper
from teatree.core.management.commands._workspace.isolated_roots import reap_orphan_isolated_worktree_roots
from teatree.core.management.commands._workspace.preview import preview_line


class TestPreviewLine:
    def test_prefixes_under_dry_run(self) -> None:
        assert preview_line("Drop orphan database: wt_x", dry_run=True) == "WOULD Drop orphan database: wt_x"

    def test_passes_through_on_a_live_run(self) -> None:
        assert preview_line("Dropped orphan database: wt_x", dry_run=False) == "Dropped orphan database: wt_x"


class TestEmptyTicketDirPreview:
    def _workspace(self, tmp_path: Path) -> Path:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        return workspace

    def test_names_the_dir_and_leaves_it_on_disk(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        (workspace / "ac-42").mkdir()
        outcomes = WorktreeReaper(workspace).remove_empty_ticket_dirs(dry_run=True)
        assert outcomes == ["WOULD Remove empty dir: ac-42"]
        assert (workspace / "ac-42").is_dir()

    def test_a_dir_holding_real_content_is_not_previewed(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        (workspace / "ac-43" / "repo").mkdir(parents=True)
        (workspace / "ac-43" / "repo" / "file.txt").write_text("work", encoding="utf-8")
        assert WorktreeReaper(workspace).remove_empty_ticket_dirs(dry_run=True) == []

    def test_live_run_still_removes(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        (workspace / "ac-44").mkdir()
        assert WorktreeReaper(workspace).remove_empty_ticket_dirs() == ["Removed empty dir: ac-44"]
        assert not (workspace / "ac-44").exists()


class TestIsolatedRootPreview:
    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "teatree-worktrees"
        (root / "orphan-slug").mkdir(parents=True)
        return root

    def test_names_the_env_dir_and_leaves_it_on_disk(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        module = "teatree.core.management.commands._workspace.isolated_roots"
        with (
            patch(f"{module}.paths.auto_isolated_worktrees_dir", return_value=root),
            patch(f"{module}._referenced_isolated_slugs", return_value=set()),
            patch(f"{module}._has_unmappable_live_worktree", return_value=False),
        ):
            outcomes = reap_orphan_isolated_worktree_roots(dry_run=True)
        assert outcomes == ["WOULD Remove orphan isolated env dir: orphan-slug"]
        assert (root / "orphan-slug").is_dir()
