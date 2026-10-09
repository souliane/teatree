"""Symlink-target health check — edge cases for worktree readiness."""

from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayProvisioning
from teatree.core.worktree.health import _symlink_source_healthy, default_health_checks
from teatree.types import SymlinkSpec


class TestSymlinkSourceHealthy:
    def test_symlink_with_existing_source_file(self, tmp_path: Path) -> None:
        source = tmp_path / "src.txt"
        source.write_text("hi")
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is True

    def test_symlink_with_missing_source_is_unhealthy(self, tmp_path: Path) -> None:
        source = tmp_path / "absent"
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is False

    def test_symlink_to_empty_directory_is_unhealthy(self, tmp_path: Path) -> None:
        source = tmp_path / "empty-dir"
        source.mkdir()
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is False

    def test_symlink_to_populated_directory_is_healthy(self, tmp_path: Path) -> None:
        source = tmp_path / "full-dir"
        source.mkdir()
        (source / "child").write_text("x")
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is True

    def test_real_file_dest_is_healthy(self, tmp_path: Path) -> None:
        dest = tmp_path / "real.txt"
        dest.write_text("ok")
        assert _symlink_source_healthy(dest, tmp_path / "ignored") is True

    def test_missing_dest_is_unhealthy(self, tmp_path: Path) -> None:
        assert _symlink_source_healthy(tmp_path / "absent", tmp_path / "also-absent") is False

    def test_real_directory_dest_empty_is_unhealthy(self, tmp_path: Path) -> None:
        dest = tmp_path / "real-dir"
        dest.mkdir()
        assert _symlink_source_healthy(dest, tmp_path / "ignored") is False

    def test_real_directory_dest_with_children_is_healthy(self, tmp_path: Path) -> None:
        dest = tmp_path / "real-dir"
        dest.mkdir()
        (dest / "child").write_text("x")
        assert _symlink_source_healthy(dest, tmp_path / "ignored") is True


class _DeclaredSymlinks(OverlayProvisioning):
    def __init__(self, specs: list[SymlinkSpec]) -> None:
        self.specs = specs

    def symlinks(self, worktree: Worktree) -> list[SymlinkSpec]:
        return self.specs


class TestDefaultHealthChecks:
    """A declared symlink is CHECKED whenever there is anything to check.

    ``apply_symlinks`` creates the link whatever the source's state, so a link on
    disk with a missing source is exactly the breakage worth reporting — the check
    stays and fails. The one spec that is dropped is the one where NEITHER the dest
    nor the source exists: an overlay provisioning its own symlinks may skip a spec
    whose source its clone does not ship, and checking a dest that provision
    deliberately never created would fail a correctly provisioned worktree.
    """

    @staticmethod
    def _worktree(wt_dir: Path) -> Worktree:
        return Worktree(repo_path="repo", branch="feat-x", extra={"worktree_path": str(wt_dir)})

    def _checks(self, tmp_path: Path, *, source: Path, link_dest: bool = True) -> dict[str, bool]:
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir(exist_ok=True)
        if link_dest:
            (wt_dir / "config").symlink_to(source)
        provisioning = _DeclaredSymlinks([SymlinkSpec(path="config", source=str(source))])
        checks = default_health_checks(provisioning, self._worktree(wt_dir))
        return {check.name: check.check() for check in checks}

    def test_a_symlink_whose_source_is_gone_fails_its_check(self, tmp_path: Path) -> None:
        results = self._checks(tmp_path, source=tmp_path / "never-provisioned")

        assert results["symlink-config"] is False

    def test_a_spec_the_overlay_skipped_registers_no_check(self, tmp_path: Path) -> None:
        # Neither dest nor source: the overlay's own applier skipped an absent
        # optional source, so there is no breakage — and no dest to demand.
        results = self._checks(tmp_path, source=tmp_path / "never-provisioned", link_dest=False)

        assert "symlink-config" not in results

    def test_a_live_source_with_no_dest_still_fails_its_check(self, tmp_path: Path) -> None:
        # Non-vacuity for the skip above: a source that DOES exist means the link
        # should have been created, so its absence stays a reported failure.
        source = tmp_path / "shared"
        source.mkdir()
        (source / "settings.py").write_text("x", encoding="utf-8")

        results = self._checks(tmp_path, source=source, link_dest=False)

        assert results["symlink-config"] is False

    def test_a_symlink_whose_source_is_populated_passes_its_check(self, tmp_path: Path) -> None:
        source = tmp_path / "shared"
        source.mkdir()
        (source / "settings.py").write_text("x", encoding="utf-8")

        results = self._checks(tmp_path, source=source)

        assert results["symlink-config"] is True
