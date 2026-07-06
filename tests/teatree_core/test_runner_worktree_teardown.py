"""``WorktreeTeardownRunner.run()`` outcome mapping.

The runner backs ``execute_worktree_teardown`` and the ``worktree`` /
``workspace teardown`` CLIs. It funnels through ``cleanup_worktree`` and
must, per #877, surface a non-clean ``CleanupResult`` loudly (logs +
``detail``) instead of swallowing it into a label string the caller never
inspects (#932) — while NOT re-blocking a teardown the operator explicitly
forced (the #706/#710 force-escape contract).
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult
from teatree.core.models import Ticket, Worktree
from teatree.core.runners.worktree_teardown import WorktreeTeardownRunner

_PATCH_DOWN = "teatree.core.runners.worktree_start.docker_compose_down"
_PATCH_CLEANUP = "teatree.core.runners.worktree_teardown.cleanup_worktree"


class TestWorktreeTeardownRunner(TestCase):
    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/877",
            state=Ticket.State.MERGED,
        )
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="fix-877",
            extra={"worktree_path": "/tmp/wt/org/repo"},
        )

    def test_clean_teardown_returns_ok_with_label(self) -> None:
        wt = self._make_worktree()
        clean = CleanupResult(label="Cleaned: org/repo (fix-877)")

        with (
            patch(_PATCH_DOWN),
            patch(_PATCH_CLEANUP, return_value=clean),
        ):
            result = WorktreeTeardownRunner(wt).run()

        assert result.ok is True
        assert result.detail == "Cleaned: org/repo (fix-877)"

    def test_step_errors_surfaced_in_detail_but_not_blocking(self) -> None:
        """#877 — a non-clean result is loud (``detail``) but still ``ok`` (force-escape kept)."""
        wt = self._make_worktree()
        dirty = CleanupResult(
            label="Cleaned: org/repo (fix-877)",
            errors=["dropdb failed for wt_877: connection refused"],
        )

        with (
            patch(_PATCH_DOWN),
            patch(_PATCH_CLEANUP, return_value=dirty),
            self.assertLogs("teatree.core.runners.worktree_teardown", level="ERROR") as logs,
        ):
            result = WorktreeTeardownRunner(wt).run()

        assert result.ok is True
        assert "dropdb failed for wt_877" in result.detail
        assert "with errors" in result.detail
        assert any("dropdb failed for wt_877" in line for line in logs.output)

    def test_refusal_returns_not_ok(self) -> None:
        """A RuntimeError refusal (the #706 data-loss guard) blocks teardown."""
        wt = self._make_worktree()

        with (
            patch(_PATCH_DOWN),
            patch(_PATCH_CLEANUP, side_effect=RuntimeError("refused teardown — on NO remote (data loss)")),
        ):
            result = WorktreeTeardownRunner(wt).run()

        assert result.ok is False
        assert "on NO remote" in result.detail

    def test_snapshot_fields_restored_before_cleanup(self) -> None:
        """The snapshot of db_name/extra is restored on the row before cleanup reads them."""
        wt = self._make_worktree()
        captured: dict[str, object] = {}

        def fake_cleanup(worktree: Worktree, **_: object) -> CleanupResult:
            captured["db_name"] = worktree.db_name
            captured["extra"] = dict(worktree.extra or {})
            return CleanupResult(label="Cleaned: org/repo (fix-877)")

        with (
            patch(_PATCH_DOWN),
            patch(_PATCH_CLEANUP, side_effect=fake_cleanup),
        ):
            WorktreeTeardownRunner(
                wt,
                snapshot_db_name="wt_877",
                snapshot_extra={"worktree_path": "/tmp/snap/org/repo"},
            ).run()

        assert captured["db_name"] == "wt_877"
        assert captured["extra"] == {"worktree_path": "/tmp/snap/org/repo"}
