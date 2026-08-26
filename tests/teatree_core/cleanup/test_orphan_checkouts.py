"""Fail-closed behaviour of the shared orphan discovery/unique-work primitives (#4579).

Both consumers (the reaper and ``workspace emit``) already exercise the happy paths through
:mod:`tests.teatree_core.management.commands.test_workspace_orphan_worktrees` and
:mod:`tests.teatree_core.worktree.test_orphan_emit`. These pin the two probes' OWN
``CommandFailedError`` arms directly — an unreadable clone during discovery, and a ref that
stops resolving mid-probe — neither of which either consumer's fixtures happen to trigger.
"""

from unittest.mock import patch

from teatree.core.cleanup.orphan_checkouts import discover_orphan_checkouts, orphan_has_unique_work
from tests.teatree_core.orphan_fixture import OrphanWorktreeFixture


class TestDiscoverOrphanCheckoutsReportsAnUnreadableClone(OrphanWorktreeFixture):
    def test_an_unreadable_clone_is_a_gap_and_does_not_lose_a_readable_ones_orphans(self) -> None:
        readable_orphan = self._add_orphan("readable-orphan")
        unreadable = self.workspace / "not-a-clone"
        unreadable.mkdir()

        with patch(
            "teatree.core.cleanup.orphan_checkouts.candidate_clones",
            return_value={str(self.repo_main), str(unreadable)},
        ):
            scan = discover_orphan_checkouts(self.workspace)

        assert any(str(unreadable) in gap for gap in scan.gaps), scan.gaps
        assert {orphan.path for orphan in scan.orphans} == {str(readable_orphan)}, (
            "one unreadable clone must not hide another clone's real orphans"
        )


class TestOrphanHasUniqueWorkFailsClosed(OrphanWorktreeFixture):
    def test_a_ref_that_stops_resolving_reads_as_unique_work(self) -> None:
        """The exact 'corrupt repo, unknown ref' case the docstring names, reproduced with real git."""
        wt_path = self._add_orphan("vanishing-ref", files={"new.py": "WORK = 1\n"})
        (self.repo_main / ".git" / "refs" / "heads" / "vanishing-ref").unlink()

        assert orphan_has_unique_work(str(self.repo_main), "vanishing-ref", str(wt_path)) is True
