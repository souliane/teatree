"""Which inodes the protectors' artifact links reach, and keeping that reading fresh cheaply."""

import os
import shutil
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.cleanup.artifact_protection import ArtifactProtection
from teatree.core.cleanup.checkout_registry import scan_checkout_paths
from tests._git_repo import make_git_repo, run_git

_CACHEDIR_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55\n"


def _identity(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_dev, status.st_ino


class _ProtectionCase(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.clone = make_git_repo(tmp_path / "clone")
        self.shared = self.clone / "node_modules"
        (self.shared / "pkg").mkdir(parents=True)
        self.worktree = tmp_path / "wt"
        run_git(self.clone, "worktree", "add", "-q", "-b", "wt", str(self.worktree))

    def _read(self) -> ArtifactProtection:
        return ArtifactProtection.read(frozenset({str(self.clone), str(self.worktree)}), (self.clone,))


class TestProtects(_ProtectionCase):
    def test_a_link_protects_its_target_and_every_ancestor_of_it(self) -> None:
        (self.worktree / "node_modules").symlink_to(self.shared / "pkg")

        protection = self._read()

        assert protection.protects(_identity(self.shared / "pkg"))
        assert protection.protects(_identity(self.shared))

    def test_a_link_spelled_through_another_mount_of_the_same_tree_still_protects(self) -> None:
        # A host-written link names the host spelling; the candidate is known by the container's.
        alias = self.root / "host-spelling"
        alias.symlink_to(self.clone)
        (self.worktree / "node_modules").symlink_to(alias / "node_modules")

        assert self._read().protects(_identity(self.shared))

    def test_an_unlinked_directory_is_not_protected(self) -> None:
        other = self.clone / ".nx"
        other.mkdir()
        (self.worktree / "node_modules").symlink_to(self.shared)

        assert not self._read().protects(_identity(other))

    def test_a_dangling_link_protects_its_nearest_existing_ancestor(self) -> None:
        (self.worktree / "node_modules").symlink_to(self.shared / "vanished" / "pkg")

        protection = self._read()

        assert protection.gaps == []
        assert protection.protects(_identity(self.shared))

    def test_a_link_that_cannot_resolve_for_another_reason_is_a_gap(self) -> None:
        loop = self.worktree / "node_modules"
        loop.symlink_to(loop)

        assert any("could not resolve" in gap for gap in self._read().gaps)

    def test_a_protector_whose_root_cannot_be_listed_is_a_gap(self) -> None:
        self.worktree.chmod(0o000)
        self.addCleanup(self.worktree.chmod, 0o755)

        assert any("could not read the artifact links" in gap for gap in self._read().gaps)

    def test_a_protector_gone_under_a_walked_directory_holds_no_links(self) -> None:
        protection = ArtifactProtection.read(frozenset({str(self.root / "not-here")}), (), (self.root,))

        assert protection.gaps == []

    def test_a_protector_gone_under_a_readable_parent_this_venue_never_walked_is_a_gap(self) -> None:
        # /tmp exists in the host and the container alike, with different contents.
        protection = ArtifactProtection.read(frozenset({str(self.root / "not-here")}), ())

        assert any("cannot be observed" in gap for gap in protection.gaps), protection.gaps

    def test_a_deleted_worktree_nested_in_a_checkout_read_here_is_proven_absent(self) -> None:
        nested = self.clone / ".claude" / "worktrees" / "agent"
        run_git(self.clone, "worktree", "add", "-q", "-b", "agent", str(nested))
        shutil.rmtree(nested)

        protection = ArtifactProtection.read(frozenset({str(self.clone), str(nested)}), (self.clone,))

        assert protection.gaps == []

    def test_an_absent_worktree_whose_nearest_ancestor_cannot_be_listed_is_a_gap(self) -> None:
        if os.geteuid() == 0:
            pytest.skip("root reads any directory")
        sealed = self.root / "sealed"
        sealed.mkdir()
        sealed.chmod(0o100)
        self.addCleanup(sealed.chmod, 0o755)

        protection = ArtifactProtection.read(frozenset({str(sealed / "ticket" / "wt")}), (), (self.root,))

        assert any("cannot be observed" in gap for gap in protection.gaps), protection.gaps

    def test_a_protector_whose_parent_is_not_observable_here_is_a_gap(self) -> None:
        protection = ArtifactProtection.read(frozenset({str(self.root / "unmounted" / "wt")}), ())

        assert any("cannot be observed" in gap for gap in protection.gaps), protection.gaps

    def test_an_absent_locked_worktree_is_a_gap(self) -> None:
        locked = self.root / "locked-wt"
        run_git(self.clone, "worktree", "add", "-q", "-b", "locked-wt", str(locked))
        run_git(self.clone, "worktree", "lock", str(locked))
        locked.rename(self.root / "moved-away")

        protection = ArtifactProtection.read(frozenset({str(locked)}), (self.clone,))

        assert any(str(locked) in gap and "locked" in gap for gap in protection.gaps), protection.gaps

    def test_an_absent_locked_worktree_recorded_with_a_relative_gitdir_is_a_gap(self) -> None:
        locked = self.root / "locked-wt"
        run_git(self.clone, "worktree", "add", "-q", "-b", "locked-wt", str(locked))
        run_git(self.clone, "worktree", "lock", str(locked))
        admin = self.clone / ".git" / "worktrees" / "locked-wt"
        (admin / "gitdir").write_text(os.path.relpath(locked / ".git", admin) + "\n", encoding="utf-8")
        locked.rename(self.root / "moved-away")

        protection = ArtifactProtection.read(frozenset({str(locked)}), (self.clone,))

        assert any(str(locked) in gap and "locked" in gap for gap in protection.gaps), protection.gaps

    def test_a_worktree_whose_parent_was_deleted_inside_a_walked_directory_is_proven_absent(self) -> None:
        ticket_dir = self.root / "ticket"
        gone = ticket_dir / "wt-gone"
        run_git(self.clone, "worktree", "add", "-q", "-b", "gone", str(gone))
        shutil.rmtree(ticket_dir)

        protection = ArtifactProtection.read(frozenset({str(gone)}), (self.clone,), (self.root,))

        assert protection.gaps == []

    def test_control_a_deleted_parent_outside_every_walked_directory_is_still_a_gap(self) -> None:
        ticket_dir = self.root / "ticket"
        gone = ticket_dir / "wt-gone"
        run_git(self.clone, "worktree", "add", "-q", "-b", "gone", str(gone))
        shutil.rmtree(ticket_dir)

        protection = ArtifactProtection.read(frozenset({str(gone)}), (self.clone,), (self.clone,))

        assert any("cannot be observed" in gap for gap in protection.gaps), protection.gaps


class TestRefresh(_ProtectionCase):
    def test_a_link_planted_after_the_reading_is_seen_on_refresh(self) -> None:
        protection = self._read()
        assert not protection.protects(_identity(self.shared)), "control: nothing links it yet"

        (self.worktree / "node_modules").symlink_to(self.shared)

        assert protection.refresh() == ()
        assert protection.protects(_identity(self.shared))

    def test_a_worktree_registered_after_the_reading_is_read_on_refresh(self) -> None:
        protection = self._read()
        fresh = self.root / "fresh"
        run_git(self.clone, "worktree", "add", "-q", "-b", "fresh", str(fresh))
        (fresh / "node_modules").symlink_to(self.shared)

        protection.refresh()

        assert protection.protects(_identity(self.shared))

    def test_an_unchanged_checkout_is_not_re_read(self) -> None:
        read_at = self.worktree.lstat().st_mtime_ns
        protection = self._read()
        (self.worktree / "node_modules").symlink_to(self.shared)
        os.utime(self.worktree, ns=(read_at, read_at))

        protection.refresh()

        assert not protection.protects(_identity(self.shared)), "refresh reads only what moved"

    def test_a_worktree_locked_after_the_reading_is_a_gap_once_it_goes_absent(self) -> None:
        already_gone = self.root / "already-gone"
        protection = ArtifactProtection.read(
            frozenset({str(self.clone), str(self.worktree), str(already_gone)}), (self.clone,), (self.root,)
        )
        assert protection.gaps == [], "control: judging the earlier absence read the lock set"
        run_git(self.clone, "worktree", "lock", str(self.worktree))
        self.worktree.rename(self.root / "moved-away")

        assert any("locked" in gap for gap in protection.refresh())


class TestTaggedCacheRefresh(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _tagged_cache_with_an_sdist_git_file(self) -> Path:
        cache = self.root / ".uv-cache"
        sdist = cache / "sdists-v9" / "pkg"
        sdist.mkdir(parents=True)
        (sdist / ".git").write_text("not a gitdir pointer", encoding="utf-8")
        (cache / "CACHEDIR.TAG").write_text(_CACHEDIR_SIGNATURE, encoding="utf-8")
        return cache

    def _protection(self) -> ArtifactProtection:
        scan = scan_checkout_paths((self.root,), into_checkouts=False)
        assert scan.complete, scan.gaps
        return ArtifactProtection.read(frozenset(scan.paths), (), scan.listed)

    def test_a_new_bucket_in_a_tagged_cache_is_not_descended(self) -> None:
        cache = self._tagged_cache_with_an_sdist_git_file()
        protection = self._protection()

        (cache / "wheels-v5").mkdir()
        os.utime(cache, ns=(1, 1))

        assert protection.refresh() == ()

    def test_a_tagged_cache_appearing_under_a_walked_directory_is_not_descended(self) -> None:
        protection = self._protection()

        self._tagged_cache_with_an_sdist_git_file()
        os.utime(self.root, ns=(1, 1))

        assert protection.refresh() == ()

    def test_control_an_untagged_directory_appearing_under_a_walked_directory_is_scanned(self) -> None:
        protection = self._protection()

        odd = self.root / "odd"
        odd.mkdir()
        (odd / ".git").write_text("not a gitdir pointer", encoding="utf-8")
        os.utime(self.root, ns=(1, 1))

        assert any("could not classify" in gap for gap in protection.refresh())
