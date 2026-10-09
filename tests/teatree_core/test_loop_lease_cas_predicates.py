"""The lease CAS predicates must re-assert the ACTUAL not-live reason.

Two defects on the same seam, both invisible to the liveness layer (which had
already ruled the owner not live) and both living purely in the ``WHERE`` clause
of the reclaiming ``UPDATE``:

*   ``claim_ownership`` could not reclaim a lease whose owner was judged NOT live
    while its TTL still held — a per-loop owner past
    ``UNVERIFIABLE_OWNER_GRACE``, or any owner whose pid is provably dead. Its
    four-arm CAS matched none of them, so a replacement worker stayed blocked for
    the rest of the TTL.
*   ``evict_stale_owner`` ORed the dead-pid arm onto the lapsed-TTL one, so a
    TTL-lapsed lease a concurrent tick refreshed under the SAME (live) pid was
    still erased and handed to a second owner.
"""

import datetime as dt
from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from teatree.core.loop_lease_liveness import UNVERIFIABLE_OWNER_GRACE
from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PER_LOOP = "loop:merge"
_MASTER = "t3-master"


def _alive(*pids: int) -> AbstractContextManager[MagicMock]:
    return patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid in set(pids))


class TestClaimReclaimsANotLiveOwnerInsideItsTtl:
    """A not-live owner whose TTL has NOT lapsed is still reclaimable."""

    def test_unverifiable_per_loop_owner_past_grace_is_reclaimed(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="dead-worker",
            owner_pid=None,
            acquired_at=now - UNVERIFIABLE_OWNER_GRACE - dt.timedelta(seconds=60),
            lease_expires_at=now + dt.timedelta(minutes=26),
        )
        with _alive():
            won, owner = LoopLease.objects.claim_ownership(_PER_LOOP, session_id="replacement", owner_pid=777)
        assert won is True
        assert owner == "replacement"

    def test_dead_pid_master_owner_inside_ttl_is_reclaimed(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name=_MASTER,
            session_id="dead-master",
            owner_pid=4242,
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(minutes=26),
        )
        with _alive(777):
            won, owner = LoopLease.objects.claim_ownership(_MASTER, session_id="replacement", owner_pid=777)
        assert won is True
        assert owner == "replacement"

    def test_live_foreign_owner_still_blocks(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="busy-worker",
            owner_pid=4242,
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(minutes=26),
        )
        with _alive(4242, 777):
            won, owner = LoopLease.objects.claim_ownership(_PER_LOOP, session_id="intruder", owner_pid=777)
        assert won is False
        assert owner == "busy-worker"

    def test_snapshot_arm_does_not_clobber_a_concurrent_reclaim(self) -> None:
        """A lease that moved between the read and the write is left alone."""
        now = timezone.now()
        row = LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="dead-worker",
            owner_pid=None,
            acquired_at=now - UNVERIFIABLE_OWNER_GRACE - dt.timedelta(seconds=60),
            lease_expires_at=now + dt.timedelta(minutes=26),
        )
        # Stand in for the rival that won between our read and our write: the
        # snapshot the CAS re-asserts no longer describes the row.
        LoopLease.objects.filter(pk=row.pk).update(session_id="rival", owner_pid=999, acquired_at=timezone.now())
        with _alive(999):
            won, owner = LoopLease.objects.claim_ownership(_PER_LOOP, session_id="loser", owner_pid=777)
        assert won is False
        assert owner == "rival"


class TestEvictReassertsTheActualReason:
    def test_ttl_lapsed_owner_refreshed_under_the_same_pid_survives(self) -> None:
        now = timezone.now()
        row = LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="owner",
            owner_pid=4242,
            acquired_at=now - dt.timedelta(hours=1),
            lease_expires_at=now - dt.timedelta(minutes=1),
        )

        refreshed_at = timezone.now()

        def _refresh_between_read_and_write(pid: int) -> bool:
            LoopLease.objects.filter(pk=row.pk).update(
                acquired_at=refreshed_at,
                lease_expires_at=refreshed_at + dt.timedelta(minutes=30),
            )
            return pid == 4242

        with patch("teatree.utils.singleton.pid_alive", side_effect=_refresh_between_read_and_write):
            evicted = LoopLease.objects.evict_stale_owner(_PER_LOOP, keep_session_id="", current_pid=None)

        assert evicted == 0
        assert LoopLease.objects.get(pk=row.pk).session_id == "owner"

    def test_provably_dead_pid_is_still_evicted(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="dead",
            owner_pid=4242,
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(minutes=26),
        )
        with _alive():
            evicted = LoopLease.objects.evict_stale_owner(_PER_LOOP, keep_session_id="", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name=_PER_LOOP).session_id == ""

    def test_unverifiable_per_loop_owner_past_grace_is_evicted(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="gone",
            owner_pid=None,
            acquired_at=now - UNVERIFIABLE_OWNER_GRACE - dt.timedelta(seconds=60),
            lease_expires_at=now + dt.timedelta(minutes=26),
        )
        with _alive():
            evicted = LoopLease.objects.evict_stale_owner(_PER_LOOP, keep_session_id="", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name=_PER_LOOP).session_id == ""

    def test_lapsed_ttl_with_no_concurrent_refresh_is_evicted(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name=_PER_LOOP,
            session_id="lapsed",
            owner_pid=4242,
            acquired_at=now - dt.timedelta(hours=1),
            lease_expires_at=now - dt.timedelta(minutes=1),
        )
        with _alive(4242):
            evicted = LoopLease.objects.evict_stale_owner(_PER_LOOP, keep_session_id="", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name=_PER_LOOP).session_id == ""
