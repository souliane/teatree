"""Shared worktree path-layout + symlink-tolerant matching helpers (finding 18)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.worktree.worktree_paths import _candidate_paths, paths_match, ticket_dir_for, worktree_dir_for
from tests._mount_table import missing_mount_table, patched_mount_table


class TestCanonicalLayout(TestCase):
    """``ticket_dir_for`` / ``worktree_dir_for`` pin the one canonical layout."""

    def test_ticket_dir_is_workspace_slash_branch(self) -> None:
        assert ticket_dir_for(Path("/ws"), "42-fix") == Path("/ws/42-fix")

    def test_worktree_dir_appends_repo_leaf(self) -> None:
        assert worktree_dir_for(Path("/ws"), "42-fix", "myrepo") == Path("/ws/42-fix/myrepo")

    def test_worktree_dir_uses_only_the_repo_leaf_of_a_slug(self) -> None:
        # An ``owner/repo`` slug names the on-disk dir by its leaf only.
        assert worktree_dir_for(Path("/ws"), "42-fix", "org/myrepo") == Path("/ws/42-fix/myrepo")

    def test_worktree_dir_is_ticket_dir_plus_leaf(self) -> None:
        # The full path is the ticket dir with the repo leaf — one composed layout.
        assert worktree_dir_for(Path("/ws"), "b", "r") == ticket_dir_for(Path("/ws"), "b") / "r"


class TestPathsMatch(TestCase):
    """``paths_match`` preserves local variants and verified mount identity."""

    def test_identical_paths_match(self) -> None:
        assert paths_match("/ws/a/repo", "/ws/a/repo") is True

    def test_accepts_path_and_str(self) -> None:
        assert paths_match(Path("/ws/a/repo"), "/ws/a/repo") is True

    def test_distinct_paths_do_not_match(self) -> None:
        assert paths_match("/ws/a/repo", "/ws/b/repo") is False

    def test_substring_paths_do_not_match(self) -> None:
        # ``/ws/9`` must not match ``/ws/90`` — no candidate variant coincides.
        assert paths_match("/ws/9-fix", "/ws/90-other") is False

    def test_resolved_symlink_matches_source(self) -> None:
        # A path reached via a symlink matches its resolved target.
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            target = tmp / "target"
            target.mkdir()
            link = tmp / "link"
            link.symlink_to(target)
            assert paths_match(str(link), str(target)) is True

    def test_two_symlinks_to_one_directory_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            target = tmp / "target"
            target.mkdir()
            first = tmp / "first"
            second = tmp / "second"
            first.symlink_to(target)
            second.symlink_to(target)
            assert paths_match(first, second)

    def test_a_recorded_spelling_absent_here_matches_its_mount_alias_both_ways(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            real_root = Path(directory) / "mnt"
            (real_root / "T1" / "repo").mkdir(parents=True)
            sibling = real_root / "T2" / "repo"
            sibling.mkdir(parents=True)
            alias = "/nonexistent-t3/workspace/T1/repo"
            assert not Path(alias).exists()
            with patched_mount_table(("259:2", "/ws", str(real_root)), ("259:2", "/ws", "/nonexistent-t3/workspace")):
                assert paths_match(alias, real_root / "T1" / "repo")
                assert paths_match(real_root / "T1" / "repo", alias)
                assert not paths_match(alias, sibling)

    def test_without_a_readable_mount_table_the_two_spellings_stay_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "mnt" / "T1" / "repo"
            real.mkdir(parents=True)
            alias = "/nonexistent-t3/workspace/T1/repo"
            with patched_mount_table(
                ("259:2", "/ws", str(real.parent.parent)), ("259:2", "/ws", "/nonexistent-t3/workspace")
            ):
                assert paths_match(alias, real)
            with missing_mount_table():
                assert not paths_match(alias, real)


class TestCandidatePaths(TestCase):
    """``_candidate_paths`` builds the variant set ``paths_match`` and the DB matcher share."""

    def test_includes_the_literal_path(self) -> None:
        assert "/ws/a/repo" in _candidate_paths("/ws/a/repo")

    def test_one_mount_spelling_keeps_the_existing_candidate_set(self) -> None:
        path = "/nonexistent-t3/workspace/T1/repo"
        with patched_mount_table(("259:2", "/ws", "/nonexistent-t3/workspace")):
            assert _candidate_paths(path) == [path]
