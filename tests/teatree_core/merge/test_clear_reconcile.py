"""``reconcile_settled_clears`` — convergence to an empty backlog (#4250).

Reclassifying a merged PR out of the FAIL only moves a permanent finding one severity
down: the row still stands unconsumed and every surface still reports it. This pass is
what empties the population, and these tests pin the three properties that make it safe
to run unattended — it settles only on definite forge evidence, it never fabricates a
``MergeAudit``, and re-running it shrinks rather than repeats.
"""

from datetime import timedelta
from typing import cast

import django.test
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.merge.clear_reconcile import reconcile_settled_clears
from teatree.core.models.merge_clear import MergeAudit, MergeClear
from teatree.core.models.merge_clear_disposal import MergeClearDisposal
from tests.factories import MergeClearFactory


def _reads(state: str) -> object:
    def read(pr_url: str) -> str:
        return state

    return read


class ReconcileTests(django.test.TestCase):
    SLUG = "souliane/teatree"

    def setUp(self) -> None:
        self.now = timezone.now()

    def _standing(self, *, pr_id: int, hours: float = 100.0) -> MergeClear:
        return cast(
            "MergeClear",
            MergeClearFactory(ticket=None, pr_id=pr_id, slug=self.SLUG, issued_at=self.now - timedelta(hours=hours)),
        )

    def test_it_settles_a_merged_clear_and_writes_no_audit(self) -> None:
        clear = self._standing(pr_id=4142)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.MERGED), now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at == self.now
        assert report.settled == ["souliane/teatree#4142 (merged)"]
        # A MergeAudit means "the keystone executed this merge". Back-filling one for a
        # merge that landed outside the keystone would corrupt the signal S1-S4 read.
        assert MergeAudit.objects.count() == 0

    def test_it_settles_a_closed_clear(self) -> None:
        clear = self._standing(pr_id=4143)

        reconcile_settled_clears(read_state=_reads(PrOpenState.CLOSED), now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at == self.now

    def test_it_settles_nothing_on_unknown(self) -> None:
        clear = self._standing(pr_id=4144)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.UNKNOWN), now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert report.settled == []
        assert report.unverified == ["souliane/teatree#4144"]

    def test_it_leaves_a_genuine_stall_alone(self) -> None:
        clear = self._standing(pr_id=4145)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.OPEN), now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert report.stalled == ["souliane/teatree#4145"]

    def test_a_dry_run_persists_nothing(self) -> None:
        clear = self._standing(pr_id=4146)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.MERGED), now=self.now, dry_run=True)

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert report.settled == ["souliane/teatree#4146 (merged)"]
        assert report.lines() == ["would settle souliane/teatree#4146 (merged)"]

    def test_it_is_idempotent_and_shrinking(self) -> None:
        self._standing(pr_id=4147)

        first = reconcile_settled_clears(read_state=_reads(PrOpenState.MERGED), now=self.now)
        second = reconcile_settled_clears(read_state=_reads(PrOpenState.MERGED), now=self.now)

        assert len(first.settled) == 1
        assert second.settled == []
        assert second.lines() == ["no unconsumed merge authorisation to reconcile"]

    def test_one_unreadable_row_never_decides_the_others(self) -> None:
        self._standing(pr_id=4148, hours=200)
        settled = self._standing(pr_id=4149, hours=100)

        unreadable = RuntimeError("forge said no")

        def read(pr_url: str) -> str:
            if pr_url.endswith("4148"):
                raise unreadable
            return PrOpenState.MERGED

        report = reconcile_settled_clears(read_state=read, now=self.now)

        settled.refresh_from_db()
        assert settled.consumed_at == self.now
        assert report.unverified == ["souliane/teatree#4148"]

    def test_the_default_reader_settles_nothing(self) -> None:
        clear = self._standing(pr_id=4150)

        reconcile_settled_clears(now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at is None


class DisposePhantomTests(django.test.TestCase):
    """#4739: a CLEAR whose PR the forge says never existed is disposed, with a reason.

    The refusal it replaces was correct and stays correct for every OTHER unreadable
    case — the evidence a 404 supplies is the whole distinction, so each test below is
    paired with the control that proves an errored probe still settles nothing.
    """

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

    def test_it_disposes_the_phantom_and_records_the_reason(self) -> None:
        clear = self._row(pr_id=4242)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.ABSENT), now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at == self.now
        assert report.disposed == ["souliane/teatree#4242 (1 row(s))"]
        assert report.settled == []
        disposal = MergeClearDisposal.objects.get(clear=clear)
        assert disposal.reason == MergeClearDisposal.Reason.PHANTOM_PR
        assert "4242" in disposal.evidence

    def test_it_writes_no_merge_audit(self) -> None:
        # A MergeAudit claims the keystone executed a merge. Nothing merged here at all.
        self._row(pr_id=4242)

        reconcile_settled_clears(read_state=_reads(PrOpenState.ABSENT), now=self.now)

        assert MergeAudit.objects.count() == 0

    def test_it_disposes_the_superseded_siblings_of_the_same_pr(self) -> None:
        # The live ledger holds 256 superseded siblings behind each live phantom row; the
        # 404 is evidence about the PR NUMBER, so leaving them would keep list-clears wrong.
        older = self._row(pr_id=4242, hours=300)
        live = self._row(pr_id=4242, hours=100)
        untouched = self._row(pr_id=4343, hours=100)

        def read(pr_url: str) -> str:
            return PrOpenState.ABSENT if pr_url.endswith("4242") else PrOpenState.OPEN

        report = reconcile_settled_clears(read_state=read, now=self.now)

        older.refresh_from_db()
        live.refresh_from_db()
        untouched.refresh_from_db()
        assert older.consumed_at == self.now
        assert live.consumed_at == self.now
        assert untouched.consumed_at is None
        assert report.disposed == ["souliane/teatree#4242 (2 row(s))"]

    def test_an_unreadable_probe_still_disposes_nothing(self) -> None:
        clear = self._row(pr_id=4242)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.UNKNOWN), now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert report.disposed == []
        assert report.unverified == ["souliane/teatree#4242"]
        assert MergeClearDisposal.objects.count() == 0

    def test_a_raising_probe_still_disposes_nothing(self) -> None:
        clear = self._row(pr_id=4242)

        unreachable = RuntimeError("forge unreachable")

        def read(pr_url: str) -> str:
            raise unreachable

        report = reconcile_settled_clears(read_state=read, now=self.now)

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert report.disposed == []
        assert MergeClearDisposal.objects.count() == 0

    def test_a_dry_run_disposes_nothing_but_reports_the_row_count(self) -> None:
        clear = self._row(pr_id=4242)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.ABSENT), now=self.now, dry_run=True)

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert MergeClearDisposal.objects.count() == 0
        assert report.lines() == ["would dispose (PR absent on forge) souliane/teatree#4242 (1 row(s))"]

    def test_the_human_line_names_the_disposal(self) -> None:
        self._row(pr_id=4242)

        report = reconcile_settled_clears(read_state=_reads(PrOpenState.ABSENT), now=self.now)

        assert report.lines() == ["disposed (PR absent on forge) souliane/teatree#4242 (1 row(s))"]

    def test_it_is_idempotent_and_shrinking(self) -> None:
        self._row(pr_id=4242)

        first = reconcile_settled_clears(read_state=_reads(PrOpenState.ABSENT), now=self.now)
        second = reconcile_settled_clears(read_state=_reads(PrOpenState.ABSENT), now=self.now)

        assert len(first.disposed) == 1
        assert second.disposed == []
        assert MergeClearDisposal.objects.count() == 1

    def test_one_forge_read_per_family_not_per_row(self) -> None:
        """Two rows issued at the SAME instant are both LIVE — supersede is strict.

        So the population really can hand this pass two rows of one family, and the
        second must be neither re-probed nor reported again once the first disposal
        consumed it. Staggered timestamps would leave the older one superseded and out
        of the population entirely, which would prove nothing.
        """
        self._row(pr_id=4242, hours=100)
        self._row(pr_id=4242, hours=100)
        probed: list[str] = []

        def read(pr_url: str) -> str:
            probed.append(pr_url)
            return PrOpenState.ABSENT

        report = reconcile_settled_clears(read_state=read, now=self.now)

        assert len(probed) == 1
        assert report.disposed == ["souliane/teatree#4242 (2 row(s))"]
