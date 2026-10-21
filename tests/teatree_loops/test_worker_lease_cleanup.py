from unittest import mock

from teatree.core.models import LoopLease
from teatree.loops.worker_lease_cleanup import _reap_expired_leases, _reclaim_dead_owner_leases


def test_worker_reclaims_dead_owner_leases() -> None:
    with mock.patch.object(LoopLease.objects, "reclaim_dead_owner_leases") as reclaim:
        _reclaim_dead_owner_leases()

    reclaim.assert_called_once_with()


def test_worker_reaps_expired_work_leases() -> None:
    with mock.patch.object(LoopLease.objects, "reap_expired_leases") as reap:
        _reap_expired_leases()

    reap.assert_called_once_with()
