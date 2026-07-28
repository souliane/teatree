"""The board janitor is hosted on a NON-colleague-facing, ENABLED loop (#3841).

The mechanism existed and was reachable only by a human typing
``t3 <overlay> ticket sync-completions``, so on an unattended box it never ran.
Wiring it to the wrong host would re-create that: ``followup`` and ``review`` are
``colleague_facing``, so they are skipped under an away-class mode — exactly when
merged tickets pile up unreconciled — and both sweep loops ship disabled, so
hosting there ships it dark. These lanes pin the host's three properties, not just
that some registration exists.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.scanners.board_reconcile import BoardReconcileScanner
from teatree.loops.housekeeping.loop import MINI_LOOP as HOUSEKEEPING_LOOP
from teatree.loops.seed import DEFAULT_LOOPS

_HOST_LOOP = "housekeeping"


def _backend() -> OverlayBackends:
    overlay = MagicMock()
    overlay.get_workspace_repos.return_value = []
    return OverlayBackends(
        name="t3-teatree",
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=MagicMock(spec=MessagingBackend),
        ready_labels=("ready",),
        overlay=overlay,
    )


class TestBoardReconcileHost(TestCase):
    def test_the_scanner_is_in_the_host_loops_domain_slice(self) -> None:
        jobs = jobs_for_domain(Domain.HOUSEKEEPING, _backend(), all_backends=())

        assert any(isinstance(job.scanner, BoardReconcileScanner) for job in jobs), jobs

    def test_the_host_loops_own_job_build_includes_the_scanner(self) -> None:
        with patch("teatree.loop.global_scanner_factories._self_update_scanner", return_value=None):
            jobs = HOUSEKEEPING_LOOP.build_jobs(backends=[_backend()])

        assert HOUSEKEEPING_LOOP.name == _HOST_LOOP
        assert any(isinstance(job.scanner, BoardReconcileScanner) for job in jobs), jobs

    def test_the_host_loop_ships_enabled_and_not_colleague_facing(self) -> None:
        spec = next(loop for loop in DEFAULT_LOOPS if loop.name == _HOST_LOOP)

        assert spec.default_enabled
        assert not spec.colleague_facing
