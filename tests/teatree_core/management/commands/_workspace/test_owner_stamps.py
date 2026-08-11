"""Owner-stamp evidence — what a venue may conclude from a stamp it cannot check (#3872).

The defect these pin is not a scan bug. Run ``clean-all --dry-run`` in the container
the project mandates ``t3`` runs in and every step is correct: it scans for checkouts,
finds none of the host's, and concludes the 185 env dirs it CAN see are orphans. The
premise is false — the isolated-env root is bind-mounted, the clone holding their
owners is not — and the blindness runs both ways, so "did my scan find an owner" is
unsound as a liveness test in either venue.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree import paths
from teatree.core.cleanup import checkout_registry
from teatree.core.management.commands._workspace import owner_stamps
from teatree.core.management.commands._workspace.owner_stamps import (
    backfill_owner_stamps,
    read_owner_stamp,
    stamp_discovered_owners,
    venue_can_observe,
)
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo

_REGISTRY = "teatree.core.cleanup.checkout_registry"


class TestAnAbsentOwningCloneProducesNoGap(TestCase):
    """The premise the stamp mechanism exists for: nothing was SKIPPED, so no gap is recorded.

    The container walks every root that exists there, reads all of them, and finishes
    ``complete=True``. The host's clone is not unreadable — it is simply not in that
    filesystem, so there is no ``OSError`` to record and no skip to report. Every
    fail-closed route that keys on an incomplete scan is therefore inert here, which is
    why the reaper's safety cannot rest on one.
    """

    def setUp(self) -> None:
        self.venue = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.venue,)))

    def test_a_fully_readable_venue_missing_the_owning_clone_reports_complete(self) -> None:
        (self.venue / "mounted-but-empty").mkdir()

        registry = checkout_registry.live_checkout_paths(self.venue)

        assert registry.gaps == ()
        assert registry.complete, "an absent clone is not a skip — nothing here can fail closed on it"
        # The venue's own tree yielded nothing. Asserting the whole set is empty would
        # be environment-dependent: `candidate_clones` also offers the cwd, which is a
        # real clone under CI.
        assert [p for p in registry.paths if p.startswith(str(self.venue))] == []

    def test_the_stamp_is_what_rules_on_that_venues_env_dirs(self) -> None:
        # With no gap and no discovered checkout, the ONLY evidence left about a dir is
        # the one it carries itself — read against what this venue could have observed.
        absent_owner = self.venue / "teatree-deploy" / ".claude" / "worktrees" / "hook-python-django"
        stamp = owner_stamps.OwnerStamp(
            absent_owner,
            observable=venue_can_observe(absent_owner, (self.venue,)),
        )

        assert stamp.proof_of_life is None
        assert stamp.missing_evidence is not None
        assert "cannot see" in stamp.missing_evidence


class TestVenueCanObserve(TestCase):
    """Absence is only evidence where the venue could have seen the thing."""

    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_missing_leaf_in_a_readable_directory_is_observable(self) -> None:
        assert venue_can_observe(self.home / "deleted-checkout", (self.home,))

    def test_a_path_under_an_unmounted_subtree_is_not_observable(self) -> None:
        # The measured #3872 geometry: the env-dir root is mounted, the clone holding
        # the owners is not, so the whole `<clone>/.claude/worktrees` chain is absent.
        unmounted = self.home / "teatree-deploy" / ".claude" / "worktrees" / "hook-python-django"

        assert not venue_can_observe(unmounted, (self.home,))

    def test_a_path_outside_every_scanned_root_is_not_observable(self) -> None:
        elsewhere = Path(self.enterContext(tempfile.TemporaryDirectory())) / "checkout"
        elsewhere.parent.mkdir(exist_ok=True)

        assert not venue_can_observe(elsewhere, (self.home,))


class TestReadOwnerStamp(TestCase):
    """What a dir's own stamp says, weighed against what this venue could check."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.venue = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_dir = self.root / "slug"
        self.env_dir.mkdir()

    def test_an_unstamped_dir_reports_no_owner_and_missing_evidence(self) -> None:
        stamp = read_owner_stamp(self.env_dir, (self.venue,))

        assert stamp.owner is None
        assert stamp.proof_of_life is None
        assert "unstamped" in str(stamp.missing_evidence)

    def test_a_stamp_naming_a_live_checkout_proves_liveness(self) -> None:
        owner = self.venue / "live-checkout"
        owner.mkdir()
        paths.IsolatedEnvDir(self.env_dir).stamp_owner(owner)

        stamp = read_owner_stamp(self.env_dir, (self.venue,))

        assert stamp.owner == owner
        assert "live checkout" in str(stamp.proof_of_life)
        assert stamp.missing_evidence is None

    def test_a_visibly_absent_owner_leaves_no_proof_and_no_gap(self) -> None:
        # The one shape a deletion may rest on: the venue read the neighbourhood and
        # the checkout is not in it, so neither branch claims the dir.
        paths.IsolatedEnvDir(self.env_dir).stamp_owner(self.venue / "vanished")

        stamp = read_owner_stamp(self.env_dir, (self.venue,))

        assert stamp.proof_of_life is None
        assert stamp.missing_evidence is None


class TestStampDiscoveredOwners(TestCase):
    """Backfill writes the mapping for what was discovered, and only that."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _env_dir(self, checkout: Path) -> Path:
        env_dir = self.root / paths.isolated_slug(checkout)
        env_dir.mkdir()
        return env_dir

    def test_a_discovered_checkout_gets_its_env_dir_stamped_once(self) -> None:
        checkout = Path("/somewhere/live/repo")
        env_dir = self._env_dir(checkout)

        first = stamp_discovered_owners(frozenset({str(checkout)}), self.root)
        second = stamp_discovered_owners(frozenset({str(checkout)}), self.root)

        assert paths.IsolatedEnvDir(env_dir).owner == checkout
        assert any(env_dir.name in line for line in first)
        assert second == [], "an already-stamped dir is not reported a second time"

    def test_a_checkout_with_no_env_dir_creates_nothing(self) -> None:
        # The backfill records ownership of dirs that exist; it never mints one.
        stamp_discovered_owners(frozenset({"/somewhere/without/an/env/dir"}), self.root)

        assert list(self.root.iterdir()) == []


class TestBackfillOwnerStamps(TestCase):
    """The non-destructive half, runnable where ``clean-all`` must not be."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))
        self.clone = make_git_repo(self.workspace / "org" / "repo")
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3872")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="registered",
            extra={"worktree_path": str(self.clone), "clone_path": str(self.clone)},
        )

    def _env_dir(self, checkout: Path) -> Path:
        env_dir = self.root / paths.isolated_slug(checkout)
        (env_dir / "logs").mkdir(parents=True)
        (env_dir / "db.sqlite3").write_bytes(b"")
        return env_dir

    def test_a_visible_checkouts_env_dir_gains_its_owner(self) -> None:
        env_dir = self._env_dir(self.clone)

        report = backfill_owner_stamps(self.workspace)

        assert paths.IsolatedEnvDir(env_dir).owner == self.clone
        assert any("Stamped" in line and env_dir.name in line for line in report)

    def test_nothing_is_deleted_and_an_invisible_owners_dir_is_reported_still_unstamped(self) -> None:
        # A backfill is venue-limited by construction: it stamps what it can see and
        # leaves the rest alone, which is why it has to run in EVERY venue.
        invisible = self._env_dir(Path("/not/mounted/here/checkout"))

        report = backfill_owner_stamps(self.workspace)

        assert invisible.is_dir()
        assert paths.IsolatedEnvDir(invisible).owner is None
        assert any("1 env dir(s) still unstamped" in line for line in report)
