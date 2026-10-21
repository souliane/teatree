"""Cleanup of worker-owned loop lease rows."""


def _reclaim_dead_owner_leases() -> None:
    """Return every ``loop:<name>``/``t3-master`` lease held by a dead session to the pool (#3571).

    The worker supervisor's runtime half of the dead-owner reclaim (``run_boot_sweeps``
    owns the boot half): a loop whose owning session crashed — or whose pid was reused /
    lives in another container namespace — is otherwise SKIPped by the live worker
    forever. Lazy-imported so the module's import graph carries no Django/ORM edge.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    LoopLease.objects.reclaim_dead_owner_leases()


def _reap_expired_leases() -> None:
    """Delete long-expired work-lease rows nothing will consult again (#4253).

    ``acquire`` mints a ``work:<kind>:<hash>`` row per unit of work and nothing retires
    it, so the table grows without bound and an operator reading it cannot tell a live
    holder from hours of debris. Owner slots are never in range — their ``generation`` is
    the fencing token — so this is disjoint from the reclaim above.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    LoopLease.objects.reap_expired_leases()
