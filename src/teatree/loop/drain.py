"""Worker drain — quiesce admission, then wait for in-flight leases to clear.

The first half of drain-then-deploy (rolling / zero-downtime deploy): a deploy
must never kill an in-flight sub-agent. ``drain_worker`` flips the
``worker_quiescing`` config gate ON — after which the claim/admission chokepoint
(``TaskQuerySet.claim_next_pending`` / ``_claimable_for_target``) admits ZERO new
work — then polls the SSOT in-flight predicate
(``Task.objects.active_claim_exists``, a live CLAIMED lease) until it reads clear
or the grace ``timeout`` lapses. It NEVER stops the supervisor and never touches a
CLAIMED lease; an in-flight task keeps renewing via ``renew_lease`` and finishes.

``deploy/deploy.sh`` runs ``t3 worker drain`` before swapping the worker image; the
FRESH worker's init clears ``worker_quiescing`` so admission resumes. On a grace
overrun the deploy proceeds anyway — a still-CLAIMED task re-queues PENDING via its
lease lapse (``reclaim_orphaned_claims``) and is picked up by the fresh worker, so
no work is lost.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

QUIESCING_SETTING = "worker_quiescing"


class DrainOutcome(Enum):
    """Terminal state of a drain wait."""

    DRAINED = "drained"
    GRACE_EXCEEDED = "grace_exceeded"


@dataclass(frozen=True, slots=True)
class DrainReport:
    outcome: DrainOutcome
    waited_seconds: float
    #: The pks of tasks still CLAIMED with a live lease when the grace lapsed
    #: (empty on a clean drain).
    still_claimed: list[int] = field(default_factory=list)

    @property
    def drained(self) -> bool:
        return self.outcome is DrainOutcome.DRAINED


def set_worker_quiescing(*, value: bool, scope: str = "") -> None:
    """Write the ``worker_quiescing`` admission gate to the ``ConfigSetting`` store.

    The same durable store ``config_setting set`` / the resolver read, so the gate
    outlives the draining process and is visible to every worker/CLI reader.
    """
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM needs the app registry

    ConfigSetting.objects.set_value(QUIESCING_SETTING, value, scope=scope)


def _still_claimed_pks() -> list[int]:
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM needs the app registry

    return list(Task.objects.active_claims().order_by("pk").values_list("pk", flat=True))


def _in_flight() -> bool:
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM needs the app registry

    return Task.objects.active_claim_exists()


def drain_worker(
    *,
    timeout: int,
    poll_interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DrainReport:
    """Quiesce admission and wait for in-flight CLAIMED leases to clear.

    Sets ``worker_quiescing`` ON (so no new task is admitted), then polls the
    in-flight predicate every ``poll_interval`` seconds. Returns a
    :class:`DrainReport` with :attr:`DrainOutcome.DRAINED` as soon as no live lease
    remains, or :attr:`DrainOutcome.GRACE_EXCEEDED` (naming the still-CLAIMED pks)
    once ``timeout`` seconds elapse. The in-flight set is checked BEFORE the first
    sleep, so a quiet worker returns DRAINED immediately. ``sleep`` / ``monotonic``
    are injectable so a test can drive the wait without wall-clock time.
    """
    set_worker_quiescing(value=True)
    start = monotonic()
    while True:
        if not _in_flight():
            return DrainReport(outcome=DrainOutcome.DRAINED, waited_seconds=monotonic() - start)
        waited = monotonic() - start
        if waited >= timeout:
            return DrainReport(
                outcome=DrainOutcome.GRACE_EXCEEDED,
                waited_seconds=waited,
                still_claimed=_still_claimed_pks(),
            )
        sleep(poll_interval)


__all__ = [
    "QUIESCING_SETTING",
    "DrainOutcome",
    "DrainReport",
    "drain_worker",
    "set_worker_quiescing",
]
