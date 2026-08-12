"""``ticket reconcile-clears`` spends the authorisations whose PR already settled (#4250).

The hand-runnable half of the convergence: the sweep lane runs it on cadence, this
surface clears today's backlog now and makes the path testable end to end.
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import MergeAudit, MergeClear

_SHA = "a" * 40
_READER = "teatree.backends.loader.pr_open_state"


def _reconcile(*args: str) -> list[str]:
    return cast("list[str]", call_command("ticket", "reconcile-clears", *args))


def _standing(*, pr_id: int = 4142, slug: str = "souliane/teatree") -> MergeClear:
    return MergeClear.objects.create(
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
        issued_at=timezone.now(),
    )


def _reads(state: str) -> object:
    def read(pr_url: str) -> str:
        return state

    return read


class ReconcileClearsCommandTests(TestCase):
    def test_it_consumes_a_merged_authorisation_and_writes_no_audit(self) -> None:
        clear = _standing()

        with patch(_READER, _reads(PrOpenState.MERGED)):
            lines = _reconcile()

        clear.refresh_from_db()
        assert clear.consumed_at is not None
        assert lines == ["settled souliane/teatree#4142 (merged)"]
        assert MergeAudit.objects.count() == 0

    def test_a_dry_run_reports_without_persisting(self) -> None:
        clear = _standing(pr_id=4143)

        with patch(_READER, _reads(PrOpenState.MERGED)):
            lines = _reconcile("--dry-run")

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert lines == ["would settle souliane/teatree#4143 (merged)"]

    def test_an_open_pr_is_left_standing(self) -> None:
        clear = _standing(pr_id=4144)

        with patch(_READER, _reads(PrOpenState.OPEN)):
            lines = _reconcile()

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert lines == ["stalled (PR still open) souliane/teatree#4144"]
