from unittest.mock import patch

from teatree.core.factory.operational_health import collect_signals
from teatree.core.models import Worktree
from teatree.loop.scanners.dead_worktree_rows import DeadWorktreeRowScanner
from tests.teatree_core.worktree.test_dead_row_release import _DeadRowCase


class DeadWorktreeRowScannerTests(_DeadRowCase):
    def test_releases_only_proven_dead_row_and_reports_held_row(self) -> None:
        releasable, dead_path = self._dead_row_with_gone_ref()
        held_path = self._worktree_on("holds", commit="feat: never pushed")
        held = self._register(held_path, branch="holds")
        self._break_checkout(held_path)

        with patch("teatree.loop.scanners.dead_worktree_rows.clone_root", return_value=self.workspace):
            signals = DeadWorktreeRowScanner().scan()
        with patch("teatree.core.factory.dead_worktree_health.clone_root", return_value=self.workspace):
            health = [signal for signal in collect_signals().signals if signal.kind == "dead_worktree_row"]

        assert not Worktree.objects.filter(pk=releasable.pk).exists()
        assert Worktree.objects.filter(pk=held.pk).exists()
        assert dead_path.is_dir()
        assert held_path.is_dir()
        assert [signal.kind for signal in signals] == ["dead_worktree_rows.released"]
        assert len(health) == 1
        assert health[0].fingerprint == f"dead-worktree:{held.pk}:holds-work"
