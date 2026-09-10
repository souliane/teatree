"""``ticket list-clears`` enumerates the authorisations every other surface filters out.

The command is the only read of the CLEAR ledger that does NOT narrow to what can
still merge, so the assertion that carries the weight is the contrast: a row
``reconcile-clears`` reports nothing about must appear here, named by standing.
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.factory.merge_backlog import ClearStanding, OutstandingClear
from teatree.core.models import MergeClear

_SHA = "a" * 40


def _list_clears(*args: str) -> list[OutstandingClear]:
    return cast("list[OutstandingClear]", call_command("ticket", "list-clears", *args))


def _reconcile(*args: str) -> list[str]:
    return cast("list[str]", call_command("ticket", "reconcile-clears", *args))


def _mis_issued(*, pr_id: int, reviewer_identity: str = "cold-reviewer") -> MergeClear:
    return MergeClear.objects.create(
        pr_id=pr_id,
        slug="souliane/teatree",
        reviewed_sha=_SHA,
        reviewer_identity=reviewer_identity,
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
        issued_at=timezone.now(),
    )


class ListClearsCommandTests(TestCase):
    def test_it_lists_an_incomplete_row_that_reconcile_clears_cannot_see(self) -> None:
        clear = _mis_issued(pr_id=223, reviewer_identity="")

        assert _reconcile("--dry-run") == ["no unconsumed merge authorisation to reconcile"]

        rows = _list_clears()
        assert [(row.pk, row.standing) for row in rows] == [(clear.pk, ClearStanding.INCOMPLETE)]

    def test_it_reports_an_empty_ledger_without_inventing_a_row(self) -> None:
        assert _list_clears() == []

    def test_a_live_authorisation_is_named_live(self) -> None:
        clear = _mis_issued(pr_id=4142)

        rows = _list_clears()
        assert [(row.pk, row.standing) for row in rows] == [(clear.pk, ClearStanding.LIVE)]
        assert rows[0].ref == "souliane/teatree#4142"

    def test_it_never_mutates_the_ledger(self) -> None:
        clear = _mis_issued(pr_id=4143, reviewer_identity="")

        _list_clears()

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert MergeClear.objects.filter(consumed_at__isnull=True).count() == 1


class ProbePhantomTests(TestCase):
    """``--probe`` asks the forge which live rows point at a PR that does not exist (#4739).

    Off by default: the unnarrowed read stays free, and a phantom is only distinguishable
    from a genuine standing authorisation by a forge call somebody has to pay for.
    """

    _READER = "teatree.backends.loader.pr_open_state"

    @staticmethod
    def _reads(state: str) -> object:
        def read(pr_url: str) -> str:
            return state

        return read

    def test_probe_retags_a_live_row_whose_pr_is_absent(self) -> None:
        clear = _mis_issued(pr_id=4242)

        with patch(self._READER, self._reads(PrOpenState.ABSENT)):
            rows = _list_clears("--probe")

        assert [(row.pk, row.standing) for row in rows] == [(clear.pk, ClearStanding.PHANTOM)]

    def test_an_unreadable_probe_leaves_the_row_live(self) -> None:
        clear = _mis_issued(pr_id=4242)

        with patch(self._READER, self._reads(PrOpenState.UNKNOWN)):
            rows = _list_clears("--probe")

        assert [(row.pk, row.standing) for row in rows] == [(clear.pk, ClearStanding.LIVE)]

    def test_without_probe_no_forge_call_is_made(self) -> None:
        _mis_issued(pr_id=4242)

        with patch(self._READER) as reader:
            rows = _list_clears()

        reader.assert_not_called()
        assert [row.standing for row in rows] == [ClearStanding.LIVE]

    def test_probe_never_mutates_the_ledger(self) -> None:
        clear = _mis_issued(pr_id=4242)

        with patch(self._READER, self._reads(PrOpenState.ABSENT)):
            _list_clears("--probe")

        clear.refresh_from_db()
        assert clear.consumed_at is None
