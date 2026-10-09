"""Shared worktree path-layout + symlink-tolerant matching helpers (finding 18)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.worktree.worktree_paths import _candidate_paths, paths_match, ticket_dir_for, worktree_dir_for


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
    """``paths_match`` is symlink-tolerant pairwise equality wrapping ``_candidate_paths``."""

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
        # A path reached via a symlink matches its resolved target (the macOS
        # ``/var`` → ``/private/var`` case generalised) — bare ``.resolve() ==``
        # misses it only across the ``/private`` literal, which the variant set covers.
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            target = tmp / "target"
            target.mkdir()
            link = tmp / "link"
            link.symlink_to(target)
            assert paths_match(str(link), str(target)) is True


class TestCandidatePaths(TestCase):
    """``_candidate_paths`` builds the variant set ``paths_match`` and the DB matcher share."""

    def test_includes_the_literal_path(self) -> None:
        assert "/ws/a/repo" in _candidate_paths("/ws/a/repo")

    def test_private_prefix_stripped_variant(self) -> None:
        out = _candidate_paths("/private/var/folders/x")
        assert "/var/folders/x" in out
