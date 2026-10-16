from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Worktree
from teatree.core.models.checkout_custody import Custody
from teatree.core.models.worktree_occupancy import TenureOptions, acquire, release
from tests.factories import WorktreeFactory


class CheckoutCustodyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.worktree = WorktreeFactory(
            extra={"attested_state": "anchor", "checkout_custody": {"anchor": "anchor", "tenures": []}}
        )

    def custody(self) -> Custody:
        self.worktree.refresh_from_db()
        custody = Custody.from_extra(self.worktree.get_extra())
        assert custody is not None
        return custody

    def test_prior_tenures_nested_requires_nonempty_uniform_chain(self) -> None:
        assert not Custody("anchor", ()).prior_tenures_nested
        acquire(self.worktree, holder="task:1", tenure=TenureOptions(namespace="pid:[42]"))
        assert not self.custody().prior_tenures_nested
        release(self.worktree, holder="task:1")
        acquire(self.worktree, holder="task:2")
        assert self.custody().prior_tenures_nested
        release(self.worktree, holder="task:2")
        acquire(self.worktree, holder="task:3")
        assert not self.custody().prior_tenures_nested

    def test_namespace_is_kept_by_renewal_and_legacy_tenure_is_strict(self) -> None:
        acquire(self.worktree, holder="task:1", tenure=TenureOptions(namespace="pid:[42]"))
        acquire(self.worktree, holder="task:1", tenure=TenureOptions(renewal=True))
        assert self.custody().tenures[0].namespace == "pid:[42]"
        extra = self.worktree.get_extra()
        del extra["checkout_custody"]["tenures"][0]["namespace"]
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        assert self.custody().tenures[0].namespace == ""
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][0]["namespace"] = 42
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        assert Custody.from_extra(self.worktree.get_extra()) is None

    def test_renewal_keeps_one_tenure(self) -> None:
        acquire(self.worktree, holder="task:1")
        acquire(self.worktree, holder="task:1", tenure=TenureOptions(renewal=True))
        assert len(self.custody().tenures) == 1

    def test_new_run_with_same_identity_abandons_the_old_tenure(self) -> None:
        acquire(self.worktree, holder="task:1")
        acquire(self.worktree, holder="task:1")
        entries = self.custody().tenures
        assert len(entries) == 2
        assert entries[0].ended == "abandoned"
        assert entries[1].ended == ""

    def test_expired_holder_is_abandoned_on_takeover(self) -> None:
        acquire(self.worktree, holder="task:1")
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        acquire(self.worktree, holder="task:2")
        assert [entry.ended for entry in self.custody().tenures] == ["abandoned", ""]

    def test_release_closes_then_attested_release_reanchors(self) -> None:
        acquire(self.worktree, holder="task:1")
        assert release(self.worktree, holder="task:1")
        assert self.custody().tenures[-1].ended == "released"
        acquire(self.worktree, holder="task:2")
        assert release(self.worktree, holder="task:2", attested_state="new")
        assert self.custody() == Custody("new", ())

    def test_invalid_anchor_never_opens_a_tenure(self) -> None:
        self.worktree.merge_extra(set_keys={"attested_state": "changed"})
        acquire(self.worktree, holder="task:1")
        self.worktree.refresh_from_db()
        assert Custody.from_extra(self.worktree.get_extra()) is None
        assert self.worktree.get_extra()["checkout_custody"]["tenures"] == []

    def test_overflow_invalidates_the_chain(self) -> None:
        for index in range(17):
            holder = f"task:{index}"
            acquire(self.worktree, holder=holder)
            release(self.worktree, holder=holder)
        self.worktree.refresh_from_db()
        assert Custody.from_extra(self.worktree.get_extra()) is None
