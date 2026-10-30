from unittest.mock import patch

from teatree.core.models import KnownIssue, Worktree
from teatree.core.worktree.dead_row_release import DeadRowDisposition, DeadRowReleaseOutcome, DeadRowVerdict
from teatree.loop.scanners.dead_worktree_rows import DeadWorktreeRowScanner
from tests.teatree_core.worktree.test_dead_row_release import _DeadRowCase


class DeadWorktreeRowScannerTests(_DeadRowCase):
    def test_releases_only_proven_dead_row_and_reports_held_row(self) -> None:
        releasable, dead_path = self._dead_row_with_gone_ref()
        held_path = self._worktree_on("holds", commit="feat: never pushed")
        held = self._register(held_path, branch="holds")
        self._break_checkout(held_path)
        second_path = self._worktree_on("holds-again", commit="feat: also never pushed")
        second = self._register(second_path, branch="holds-again")
        self._break_checkout(second_path)

        with patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace):
            signals = DeadWorktreeRowScanner().scan()
        health = list(KnownIssue.objects.open().filter(kind="dead_worktree_row"))

        assert not Worktree.objects.filter(pk=releasable.pk).exists()
        assert Worktree.objects.filter(pk=held.pk).exists()
        assert Worktree.objects.filter(pk=second.pk).exists()
        assert dead_path.is_dir()
        assert held_path.is_dir()
        assert [signal.kind for signal in signals] == ["dead_worktree_rows.released"]
        assert len(health) == 1
        assert health[0].fingerprint == "dead-worktree:holds-work"
        assert str(held.pk) in health[0].summary
        assert str(second.pk) in health[0].summary

        Worktree.objects.filter(pk=held.pk).delete()
        Worktree.objects.filter(pk=second.pk).delete()
        with patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace):
            DeadWorktreeRowScanner().scan()
        assert not KnownIssue.objects.open().filter(kind="dead_worktree_row").exists()

    def test_disposition_summary_fits_the_health_registry_field(self) -> None:
        verdict = DeadRowVerdict(
            worktree_pk=123,
            branch="held",
            path="/missing",
            disposition=DeadRowDisposition.PROTECTED,
            reason="unverifiable " * 100,
        )
        outcome = DeadRowReleaseOutcome(applied=True, verdicts=(verdict,), released_pks=frozenset())
        with patch("teatree.loop.scanners.dead_worktree_rows.release_dead_rows", return_value=outcome):
            DeadWorktreeRowScanner().scan()
        summary = KnownIssue.objects.get(fingerprint="dead-worktree:protected").summary
        assert len(summary) <= KnownIssue._meta.get_field("summary").max_length
