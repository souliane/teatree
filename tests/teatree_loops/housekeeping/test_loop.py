"""The housekeeping loop's registered global jobs, run against real registry rows and checkouts."""

from unittest.mock import patch

from teatree.core.models import Ticket, Worktree
from teatree.loops.housekeeping.loop import MINI_LOOP
from tests.teatree_core.worktree.test_dead_row_release import _DeadRowCase


class GlobalJobsReleaseDeadRowsTest(_DeadRowCase):
    def _run_global_dead_row_job(self) -> None:
        (job,) = [
            job for job in MINI_LOOP.build_jobs() if job.overlay == "" and job.scanner.name == "dead_worktree_rows"
        ]
        with patch("teatree.loop.scanners.dead_worktree_rows.worktree_root", return_value=self.workspace):
            job.scanner.scan()

    def test_one_run_releases_the_ignored_ticket_row_and_keeps_the_live_and_pinned_rows(self) -> None:
        dead, dead_path = self._dead_row_with_gone_ref("ignored")
        Ticket.objects.filter(pk=dead.ticket_id).update(state=Ticket.State.IGNORED)
        live_path = self._worktree_on("live", commit="feat: in progress")
        live = self._register(live_path, branch="live")
        pinned, pinned_path = self._dead_row_with_gone_ref("pinned")
        pinned.extra = {**pinned.extra, "reaper_pinned": True}
        pinned.save(update_fields=["extra"])

        self._run_global_dead_row_job()

        assert not Worktree.objects.filter(pk=dead.pk).exists()
        assert dead_path.is_dir()
        assert Worktree.objects.filter(pk=live.pk).exists()
        assert Worktree.objects.filter(pk=pinned.pk).exists()
        assert pinned_path.is_dir()
