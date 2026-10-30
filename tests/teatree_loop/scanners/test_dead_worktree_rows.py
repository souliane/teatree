from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from teatree.core.factory.operational_health import collect_signals
from teatree.core.models import KnownIssue, Worktree
from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.loop.scanners.dead_worktree_rows import DeadWorktreeRowScanner
from tests.teatree_core.worktree.test_dead_row_release import _DeadRowCase


class DeadWorktreeRowScannerTests(_DeadRowCase):
    def test_recent_pass_skips_reclassification(self) -> None:
        with (
            patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace),
            patch("teatree.loop.scanners.dead_worktree_rows.release_dead_rows") as release,
        ):
            release.return_value.verdicts = []
            release.return_value.released_pks = []
            scanner = DeadWorktreeRowScanner()
            assert scanner.scan() == []
            assert scanner.scan() == []
        release.assert_called_once()

    def test_expired_cadence_rechecks_rows(self) -> None:
        with (
            patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace),
            patch("teatree.loop.scanners.dead_worktree_rows.release_dead_rows") as release,
        ):
            release.return_value.verdicts = []
            release.return_value.released_pks = []
            scanner = DeadWorktreeRowScanner()
            scanner.scan()
            marker = ResourcePressureMarker.load()
            marker.last_dead_worktree_scan_at = timezone.now() - timedelta(minutes=31)
            marker.save(update_fields=["last_dead_worktree_scan_at"])
            scanner.scan()
        assert release.call_count == 2

    def test_releases_only_proven_dead_row_and_reports_held_row(self) -> None:
        releasable, dead_path = self._dead_row_with_gone_ref()
        held_path = self._worktree_on("holds", commit="feat: never pushed")
        held = self._register(held_path, branch="holds")
        self._break_checkout(held_path)

        with patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace):
            signals = DeadWorktreeRowScanner(cadence_minutes=0).scan()
        health = list(KnownIssue.objects.open().filter(kind="dead_worktree_row"))

        assert not Worktree.objects.filter(pk=releasable.pk).exists()
        assert Worktree.objects.filter(pk=held.pk).exists()
        assert dead_path.is_dir()
        assert held_path.is_dir()
        assert [signal.kind for signal in signals] == ["dead_worktree_rows.released"]
        assert len(health) == 1
        assert health[0].fingerprint == f"dead-worktree:{held.pk}:holds-work"

        with patch("teatree.core.worktree.dead_row_release._plan_rows", side_effect=AssertionError):
            assert "dead_worktree_row_signals" not in collect_signals().unread

        Worktree.objects.filter(pk=held.pk).delete()
        with patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace):
            DeadWorktreeRowScanner(cadence_minutes=0).scan()
        assert not KnownIssue.objects.open().filter(kind="dead_worktree_row").exists()
