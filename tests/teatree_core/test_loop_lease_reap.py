"""``LoopLeaseQuerySet.reap_expired_leases`` — retiring work-lease debris (#4253).

``acquire`` mints a ``work:<kind>:<hash>`` row per unit of work and nothing retired it,
so the table only grew: 123 of 182 rows on one box, every sampled one expired hours
earlier. Individually harmless — an expired lease blocks no claim — but collectively the
table stops being readable, and an operator diagnosing a stuck slot cannot tell a live
holder from a day of debris without checking every expiry by hand.

The reap is deliberately narrow, and each boundary is pinned below: owner slots keep
their §5 fencing ``generation``, a held row is never touched at any expiry, and the grace
is far enough past any lease TTL that a row a caller is about to renew is unreachable.
"""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.loop_lease_manager import EXPIRED_LEASE_REAP_GRACE, PER_LOOP_OWNER_PREFIX, T3_MASTER_SLOT
from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_LONG_AGO = EXPIRED_LEASE_REAP_GRACE + dt.timedelta(hours=1)


def _lease(name: str, *, session_id: str = "", expired_for: dt.timedelta | None) -> LoopLease:
    now = timezone.now()
    return LoopLease.objects.create(
        name=name,
        session_id=session_id,
        acquired_at=now,
        lease_expires_at=None if expired_for is None else now - expired_for,
    )


class TestReapExpiredLeases:
    def test_a_long_expired_unheld_work_lease_is_deleted(self) -> None:
        _lease("work:pr:abc123", expired_for=_LONG_AGO)

        assert LoopLease.objects.reap_expired_leases() == 1
        assert not LoopLease.objects.filter(name="work:pr:abc123").exists()

    def test_a_recently_expired_row_is_inside_the_grace_and_kept(self) -> None:
        # The grace is an order of magnitude past the 1800s owner TTL, so a row a caller
        # is about to renew is never deleted out from under it.
        _lease("work:issue:def456", expired_for=dt.timedelta(minutes=5))

        assert LoopLease.objects.reap_expired_leases() == 0
        assert LoopLease.objects.filter(name="work:issue:def456").exists()

    def test_a_row_still_held_by_a_session_is_never_reaped(self) -> None:
        _lease("work:pr:held", session_id="a-live-session", expired_for=_LONG_AGO)

        assert LoopLease.objects.reap_expired_leases() == 0
        assert LoopLease.objects.filter(name="work:pr:held").exists()

    def test_a_row_with_no_expiry_at_all_is_never_reaped(self) -> None:
        _lease("work:pr:noexpiry", expired_for=None)

        assert LoopLease.objects.reap_expired_leases() == 0
        assert LoopLease.objects.filter(name="work:pr:noexpiry").exists()

    def test_the_master_owner_slot_keeps_its_fencing_token(self) -> None:
        # Deleting an owner row resets ``generation`` to 0, and a fencing token that goes
        # backwards un-fences an already-fenced worker.
        _lease(T3_MASTER_SLOT, expired_for=_LONG_AGO)

        assert LoopLease.objects.reap_expired_leases() == 0
        assert LoopLease.objects.filter(name=T3_MASTER_SLOT).exists()

    def test_a_per_loop_owner_slot_keeps_its_fencing_token(self) -> None:
        slot = f"{PER_LOOP_OWNER_PREFIX}dispatch"
        _lease(slot, expired_for=_LONG_AGO)

        assert LoopLease.objects.reap_expired_leases() == 0
        assert LoopLease.objects.filter(name=slot).exists()

    def test_it_reaps_only_the_debris_out_of_a_mixed_table(self) -> None:
        _lease("work:pr:one", expired_for=_LONG_AGO)
        _lease("work:pr:two", expired_for=_LONG_AGO)
        _lease("work:pr:fresh", expired_for=dt.timedelta(minutes=1))
        _lease(T3_MASTER_SLOT, expired_for=_LONG_AGO)

        assert LoopLease.objects.reap_expired_leases() == 2
        assert sorted(LoopLease.objects.values_list("name", flat=True)) == [T3_MASTER_SLOT, "work:pr:fresh"]

    def test_the_grace_is_caller_overridable_for_a_deliberate_sweep(self) -> None:
        _lease("work:pr:recent", expired_for=dt.timedelta(minutes=5))

        assert LoopLease.objects.reap_expired_leases(older_than=dt.timedelta(minutes=1)) == 1
