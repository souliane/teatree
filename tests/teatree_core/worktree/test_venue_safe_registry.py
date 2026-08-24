"""The venue gate over a clone's worktree registry (souliane/teatree#4287).

Real ``git worktree`` registrations under ``tmp_path`` against a real clone, so
the assertions are about what ``git worktree prune`` actually did to the admin
dirs — not about a mocked decision. The canonical worktree root is pinned per
test, which is what makes "this venue owns that path" a controllable fact.
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core.worktree.venue_safe_registry import (
    WorkPresence,
    prune_refusal,
    prune_worktrees,
    registrations,
    unprovable_registrations,
    unsalvageable_work_state,
    venue_may_call_absent_dead,
    worktree_branches,
    worktree_map,
)
from teatree.utils import git


class _RegistryTestCase(TestCase):
    """A clone with two linked worktrees: one inside the canonical root, one outside."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.canonical = self.tmp / "canonical"
        self.canonical.mkdir()
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        git.run(repo=str(self.clone), args=["init", "--quiet", "--initial-branch=main"])
        git.run(repo=str(self.clone), args=["config", "user.email", "t@example.com"])
        git.run(repo=str(self.clone), args=["config", "user.name", "t"])
        git.run(repo=str(self.clone), args=["commit", "--quiet", "--allow-empty", "-m", "init"])
        patch = mock.patch(
            "teatree.core.worktree.venue_safe_registry.canonical_worktree_root",
            return_value=self.canonical,
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _add_worktree(self, path: Path, branch: str) -> Path:
        git.run(repo=str(self.clone), args=["worktree", "add", "--quiet", "-b", branch, str(path)])
        return path

    def _admin_entries(self) -> set[str]:
        admin = self.clone / ".git" / "worktrees"
        return {entry.name for entry in admin.iterdir()} if admin.is_dir() else set()

    def _remove_tree(self, path: Path) -> None:
        shutil.rmtree(path)


class PruneGateTest(_RegistryTestCase):
    def test_a_registration_this_venue_owns_and_proved_gone_is_pruned(self) -> None:
        inside = self._add_worktree(self.canonical / "owned", "owned")
        self._remove_tree(inside)

        assert prune_worktrees(str(self.clone)) == ""
        assert self._admin_entries() == set()

    def test_a_registration_this_venue_cannot_stat_survives_the_pass(self) -> None:
        unreachable = self.tmp / "unreachable" / "checkout"
        unreachable.parent.mkdir()
        self._add_worktree(unreachable, "unreachable")
        self._remove_tree(unreachable.parent)

        refusal = prune_worktrees(str(self.clone))

        assert "unreadable in this execution context" in refusal
        assert self._admin_entries() == {"checkout"}

    def test_an_absent_path_with_a_readable_parent_outside_the_root_still_refuses(self) -> None:
        """The shape a bare ``observe`` misses: a host checkout under a mounted parent."""
        outside = self.tmp / "elsewhere"
        self._add_worktree(outside, "elsewhere")
        self._remove_tree(outside)

        refusal = prune_worktrees(str(self.clone))

        assert str(outside) in refusal
        assert self._admin_entries() == {"elsewhere"}

    def test_a_locked_registration_does_not_withhold_the_prune(self) -> None:
        outside = self.tmp / "locked-elsewhere"
        self._add_worktree(outside, "locked-elsewhere")
        git.run(repo=str(self.clone), args=["worktree", "lock", "--reason", "not mounted here", str(outside)])
        dead = self._add_worktree(self.canonical / "dead", "dead")
        self._remove_tree(outside)
        self._remove_tree(dead)

        assert prune_worktrees(str(self.clone)) == ""
        assert self._admin_entries() == {"locked-elsewhere"}

    def test_an_unreadable_registry_refuses_rather_than_pruning_an_unknown_scope(self) -> None:
        refusal = prune_refusal(str(self.tmp / "not-a-clone"))

        assert "could not read the worktree registry" in refusal

    def test_a_present_checkout_outside_the_root_never_withholds_the_prune(self) -> None:
        self._add_worktree(self.tmp / "live-elsewhere", "live-elsewhere")

        assert unprovable_registrations(str(self.clone)) == []


class RegistrationParseTest(_RegistryTestCase):
    def test_branch_path_and_lock_state_are_read_off_the_porcelain(self) -> None:
        linked = self._add_worktree(self.canonical / "linked", "linked")
        git.run(repo=str(self.clone), args=["worktree", "lock", str(linked)])

        by_path = {entry.path: entry for entry in registrations(str(self.clone))}

        assert by_path[str(linked)].branch == "linked"
        assert by_path[str(linked)].locked is True
        assert by_path[str(self.clone)].locked is False

    def test_worktree_map_and_branches_cover_the_main_checkout_and_its_links(self) -> None:
        linked = self._add_worktree(self.canonical / "linked", "linked")

        assert worktree_map(str(self.clone)) == {"main": str(self.clone), "linked": str(linked)}
        assert worktree_branches(str(self.clone)) == {"main", "linked"}

    def test_an_unreadable_registry_maps_to_nothing_rather_than_raising(self) -> None:
        assert worktree_map(str(self.tmp / "not-a-clone")) == {}


class VenueMayCallAbsentDeadTest(_RegistryTestCase):
    def test_an_absent_path_under_the_canonical_root_is_proved_dead(self) -> None:
        assert venue_may_call_absent_dead(self.canonical / "gone") is True

    def test_an_absent_path_outside_the_canonical_root_is_missing_evidence(self) -> None:
        assert venue_may_call_absent_dead(self.tmp / "gone") is False

    def test_a_path_whose_neighbourhood_is_unreadable_is_missing_evidence(self) -> None:
        assert venue_may_call_absent_dead(self.canonical / "never-mounted" / "gone") is False


class UnsalvageableWorkStateTest(_RegistryTestCase):
    def test_uncommitted_changes_hold_work(self) -> None:
        checkout = self._add_worktree(self.canonical / "dirty", "dirty")
        (checkout / "scratch.txt").write_text("unsaved", encoding="utf-8")

        assert unsalvageable_work_state(str(checkout)) is WorkPresence.HOLDS_WORK

    def test_a_tip_on_no_remote_holds_work(self) -> None:
        checkout = self._add_worktree(self.canonical / "local-tip", "local-tip")
        git.run(repo=str(checkout), args=["commit", "--quiet", "--allow-empty", "-m", "local only"])

        assert unsalvageable_work_state(str(checkout)) is WorkPresence.HOLDS_WORK

    def test_a_clean_pushed_checkout_holds_nothing(self) -> None:
        checkout = self._add_worktree(self.canonical / "pushed", "pushed")
        git.run(repo=str(checkout), args=["update-ref", "refs/remotes/origin/pushed", "HEAD"])

        assert unsalvageable_work_state(str(checkout)) is WorkPresence.NONE

    def test_an_absent_checkout_this_venue_owns_holds_nothing(self) -> None:
        assert unsalvageable_work_state(str(self.canonical / "gone")) is WorkPresence.NONE

    def test_an_unreadable_checkout_is_unknown_never_no_work(self) -> None:
        assert unsalvageable_work_state(str(self.tmp / "elsewhere" / "gone")) is WorkPresence.UNKNOWN

    def test_an_absent_checkout_outside_the_root_is_unknown(self) -> None:
        assert unsalvageable_work_state(str(self.tmp / "gone")) is WorkPresence.UNKNOWN
