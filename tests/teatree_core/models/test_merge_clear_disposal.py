"""``MergeClearDisposal`` — why a standing merge authorisation was spent (#4739).

``reconcile-clears`` refuses to consume a CLEAR without forge evidence, which is right,
and left a CLEAR whose PR never existed standing forever: the evidence it waits for can
never arrive. Disposal is the third outcome, and it is recorded rather than silent —
deleting the row would erase the fact that something once authorised a merge.
"""

from datetime import timedelta
from typing import cast

import django.test
from django.utils import timezone

from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.merge_clear_disposal import MergeClearDisposal
from tests.factories import MergeClearFactory

_URL = "https://github.com/souliane/teatree/pull/4242"


class DisposePhantomFamilyTests(django.test.TestCase):
    SLUG = "souliane/teatree"

    def setUp(self) -> None:
        self.now = timezone.now()

    def _row(self, *, pr_id: int, hours: float = 100.0, consumed: bool = False) -> MergeClear:
        return cast(
            "MergeClear",
            MergeClearFactory(
                ticket=None,
                pr_id=pr_id,
                slug=self.SLUG,
                issued_at=self.now - timedelta(hours=hours),
                consumed_at=self.now if consumed else None,
            ),
        )

    def test_it_consumes_every_unconsumed_row_of_the_family(self) -> None:
        first = self._row(pr_id=4242, hours=300)
        second = self._row(pr_id=4242, hours=100)

        disposed = MergeClearDisposal.objects.dispose_phantom_family(first, pr_url=_URL, now=self.now)

        first.refresh_from_db()
        second.refresh_from_db()
        assert {row.pk for row in disposed} == {first.pk, second.pk}
        assert first.consumed_at == self.now
        assert second.consumed_at == self.now
        assert MergeClearDisposal.objects.count() == 2

    def test_it_leaves_another_prs_rows_alone(self) -> None:
        phantom = self._row(pr_id=4242)
        other = self._row(pr_id=4343)

        MergeClearDisposal.objects.dispose_phantom_family(phantom, pr_url=_URL, now=self.now)

        other.refresh_from_db()
        assert other.consumed_at is None
        assert not MergeClearDisposal.objects.filter(clear=other).exists()

    def test_it_leaves_an_already_consumed_row_alone(self) -> None:
        # A consumed row was spent by a real merge; re-stamping it would rewrite that history.
        spent = self._row(pr_id=4242, consumed=True)
        spent_at = spent.consumed_at
        live = self._row(pr_id=4242)

        MergeClearDisposal.objects.dispose_phantom_family(live, pr_url=_URL, now=self.now)

        spent.refresh_from_db()
        assert spent.consumed_at == spent_at
        assert not MergeClearDisposal.objects.filter(clear=spent).exists()

    def test_a_second_pass_disposes_nothing(self) -> None:
        clear = self._row(pr_id=4242)

        MergeClearDisposal.objects.dispose_phantom_family(clear, pr_url=_URL, now=self.now)
        again = MergeClearDisposal.objects.dispose_phantom_family(clear, pr_url=_URL, now=self.now)

        assert again == []
        assert MergeClearDisposal.objects.count() == 1

    def test_it_records_the_reason_and_the_probed_url(self) -> None:
        clear = self._row(pr_id=4242)

        MergeClearDisposal.objects.dispose_phantom_family(clear, pr_url=_URL, now=self.now)

        disposal = MergeClearDisposal.objects.get(clear=clear)
        assert disposal.reason == MergeClearDisposal.Reason.PHANTOM_PR
        assert _URL in disposal.evidence
        assert disposal.disposed_at == self.now

    def test_deleting_the_clear_takes_its_disposal_with_it(self) -> None:
        clear = self._row(pr_id=4242)
        MergeClearDisposal.objects.dispose_phantom_family(clear, pr_url=_URL, now=self.now)

        clear.delete()

        assert MergeClearDisposal.objects.count() == 0
