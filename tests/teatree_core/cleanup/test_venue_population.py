"""The two populations: what a pass may delete from, and what protects it.

Real ``git`` under a tmp tree. The candidate walk is bounded to the roots this venue
provisions into; the protectors are deliberately wider, and the tests pin that narrowing
one never narrows the other.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.cleanup.checkout_registry import scan_checkout_paths
from teatree.core.cleanup.venue_population import (
    linked_worktree_paths,
    venue_candidates,
    venue_checkout_roots,
    venue_population,
)
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git

_POPULATION = "teatree.core.cleanup.venue_population"


class _VenueCase(TestCase):
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.base = base
        self.venue = base / "venue"
        self.outside = base / "outside"
        self.outside.mkdir()
        self.clone = make_git_repo(self.venue / "org" / "repo")
        self.enterContext(patch(f"{_POPULATION}.clone_root", return_value=self.venue))
        self.enterContext(patch(f"{_POPULATION}.canonical_worktree_root", return_value=self.venue / "t3-workspaces"))

    def _row(self, path: Path) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/issues/{path.name}")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch=path.name,
            extra={"worktree_path": str(path), "clone_path": str(self.clone)},
        )


class TestVenueRoots(_VenueCase):
    def test_roots_are_where_this_venue_provisions_never_home_or_a_rows_parent(self) -> None:
        self._row(self.outside / "host-checkout")

        assert venue_checkout_roots(self.venue / "t3-workspaces") == (self.venue,)


class TestCandidates(_VenueCase):
    def test_a_checkout_is_not_walked_but_its_nested_linked_worktree_is_found(self) -> None:
        nested = self.clone / ".claude" / "worktrees" / "agent-1"
        run_git(self.clone, "worktree", "add", "-q", "-b", "agent-1", str(nested))
        unreadable = self.clone / "src" / "locked"
        unreadable.mkdir(parents=True)
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o755)

        found = venue_candidates(self.venue)

        assert found.complete, found.gaps
        assert {str(self.clone), str(nested)} <= found.candidates
        assert not scan_checkout_paths((self.venue,)).complete, "control: a full descent does read inside"

    def test_a_worktree_registered_outside_the_roots_protects_but_is_never_a_candidate(self) -> None:
        elsewhere = self.outside / "host-wt"
        run_git(self.clone, "worktree", "add", "-q", "-b", "host-wt", str(elsewhere))

        found = venue_candidates(self.venue)

        assert str(elsewhere) not in found.candidates
        assert str(elsewhere) in found.protectors
        assert any(str(elsewhere) in note and "protector only" in note for note in found.excluded)

    def test_a_worktree_git_records_under_the_real_path_of_a_symlinked_root_is_a_candidate(self) -> None:
        # The container reaches its clones through an alias; git records the path the worktree was made at.
        alias = self.base / "alias"
        alias.symlink_to(self.venue)
        real = self.venue / "t3-workspaces" / "912-x" / "repo"
        run_git(self.clone, "worktree", "add", "-q", "-b", "912-x", str(real))
        with patch(f"{_POPULATION}.clone_root", return_value=alias):
            found = venue_candidates(alias)

        assert any(Path(path).resolve() == real for path in found.candidates), found.candidates

    def test_a_row_spelled_through_a_bind_mount_alias_is_resolved_to_the_venue_checkout(self) -> None:
        # A bind mount is an alias path resolution cannot see through; only the inode says it is the same dir.
        checkout = self.venue / "t3-workspaces" / "912-x" / "repo"
        run_git(self.clone, "worktree", "add", "-q", "-b", "912-x", str(checkout))
        host_view = self.outside / "host-view"
        host_view.symlink_to(self.venue)
        row = self._row(host_view / "t3-workspaces" / "912-x" / "repo")

        with patch(f"{_POPULATION}._resolved", side_effect=lambda path: path):
            found = venue_candidates(self.venue)

        assert any(f"#{row.pk}" in note and f"resolved to {checkout}" in note for note in found.excluded)

    def test_a_row_spelled_through_a_symlinked_alias_is_inside_the_venue(self) -> None:
        checkout = self.venue / "t3-workspaces" / "912-y" / "repo"
        run_git(self.clone, "worktree", "add", "-q", "-b", "912-y", str(checkout))
        host_view = self.outside / "host-view"
        host_view.symlink_to(self.venue)
        row = self._row(host_view / "t3-workspaces" / "912-y" / "repo")

        found = venue_candidates(self.venue)

        assert not any(f"#{row.pk}" in note for note in found.excluded), found.excluded

    def test_an_out_of_venue_row_naming_nothing_here_is_skipped_with_a_reason(self) -> None:
        row = self._row(self.outside / "gone")

        found = venue_candidates(self.venue)

        assert any(f"#{row.pk}" in note and "skipped" in note for note in found.excluded)
        assert str(self.outside / "gone") not in found.candidates


class TestNestedClones(_VenueCase):
    def test_a_standalone_clone_nested_in_an_untracked_dir_is_a_protector_not_a_candidate(self) -> None:
        nested = make_git_repo(self.clone / "scratch" / "deep" / "inner")

        found = venue_population(self.venue)

        assert str(nested) in found.protectors
        assert str(nested) not in found.candidates
        assert str(nested) not in venue_candidates(self.venue).protectors, "control: only the probe finds it"

    def test_a_tagged_cache_left_untracked_in_a_candidate_is_not_probed(self) -> None:
        cache = self.clone / ".uv-cache"
        sdist = cache / "sdists-v9" / "pkg"
        sdist.mkdir(parents=True)
        (sdist / ".git").write_text("not a gitdir pointer", encoding="utf-8")
        (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8")

        found = venue_population(self.venue)

        assert found.complete, found.gaps

    def test_a_clone_nested_in_a_nested_clone_is_found(self) -> None:
        inner = make_git_repo(self.clone / "scratch" / "inner")
        innermost = make_git_repo(inner / "vendor-copy" / "innermost")

        assert str(innermost) in venue_population(self.venue).protectors

    def test_clones_nested_deeper_than_the_probe_follows_are_a_gap(self) -> None:
        level = self.clone
        for depth in range(4):
            level = make_git_repo(level / "scratch" / f"level-{depth}")

        found = venue_population(self.venue)

        assert any("levels deep" in gap for gap in found.gaps), found.gaps

    def test_a_checkout_git_cannot_probe_is_a_gap(self) -> None:
        with patch(f"{_POPULATION}.git.run_strict_verbatim", side_effect=OSError("git is gone")):
            found = venue_population(self.venue)

        assert any("could not probe" in gap for gap in found.gaps)

    def test_a_candidate_walk_that_already_refuses_skips_the_costly_probe(self) -> None:
        (self.clone / ".git" / "HEAD").unlink()
        with patch(f"{_POPULATION}.nested_clones") as probe:
            found = venue_population(self.venue)

        assert not found.complete
        probe.assert_not_called()

    def test_an_exhausted_budget_is_a_gap_rather_than_an_unprobed_pass(self) -> None:
        found = venue_population(self.venue, deadline=0.0)

        assert not found.complete
        assert any("budget" in gap for gap in found.gaps)


class TestLinkedWorktreePaths(_VenueCase):
    """The population a worktree GC may act on — never a clone."""

    def test_worktrees_under_a_non_repo_root_are_enumerated_and_the_clone_is_not(self) -> None:
        checkout = self.venue / "feat-a"
        run_git(self.clone, "worktree", "add", "-q", "-b", "feat-a", str(checkout))

        enumeration = linked_worktree_paths(self.venue)

        assert str(checkout) in enumeration.paths
        assert str(self.clone) not in enumeration.paths
        assert enumeration.complete

    def test_an_unreadable_registry_is_a_gap_not_an_empty_answer(self) -> None:
        run_git(self.clone, "worktree", "add", "-q", "-b", "feat-c", str(self.venue / "feat-c"))
        (self.clone / ".git" / "HEAD").unlink()

        enumeration = linked_worktree_paths(self.venue)

        assert not enumeration.complete
        assert any(str(self.clone) in gap for gap in enumeration.gaps)
