"""Worker drain — quiesce admission, then wait for in-flight leases to clear.

The first half of drain-then-deploy (rolling / zero-downtime deploy): a deploy
must never kill an in-flight sub-agent. ``drain_worker`` flips the
``worker_quiescing`` config gate ON — after which ``claim_admission_block_reason``
(the claim path) and ``headless_admission_block_reason`` (the auto-enqueue signal, the
queue drain and ``execute_task``) admit ZERO new work — then polls the SSOT in-flight
set (``Task.objects.active_claims``, the live CLAIMED leases) until it reads empty or
the grace ``timeout`` lapses. It NEVER stops the supervisor and never touches a
CLAIMED lease; an in-flight task keeps renewing via ``renew_lease`` and finishes.

``deploy/deploy.sh`` runs ``t3 worker drain`` before swapping the worker image; the
FRESH worker's init clears ``worker_quiescing`` so admission resumes. On a grace
overrun the deploy proceeds anyway — a still-CLAIMED task re-queues PENDING via its
lease lapse (``reclaim_orphaned_claims``) and is picked up by the fresh worker, so
no work is lost.

The wait reports a :class:`DrainProgress` sample on every poll. Waiting on in-flight
agents is inherently long, and the deploy reaches this command through an SSH session
that tears down after ~280s of silence — so a wait that says nothing cannot reach its
own 1800s budget, and the deploy dies before the swap that clears the gate (#3983).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum

from django.utils import timezone

from teatree.generation import short_sha

QUIESCING_SETTING = "worker_quiescing"


class DrainOutcome(Enum):
    """Terminal state of a drain wait."""

    DRAINED = "drained"
    GRACE_EXCEEDED = "grace_exceeded"


@dataclass(frozen=True, slots=True)
class DrainProgress:
    """One heartbeat sample of an ongoing drain wait."""

    waited_seconds: float
    #: The pks still CLAIMED with a live lease at this sample.
    still_claimed: list[int]


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


class GenerationNotDrainableError(RuntimeError):
    """The named generation is not live, so there is nothing of its to drain."""


def _still_claimed_pks(generation: str) -> list[int]:
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM needs the app registry

    claims = Task.objects.active_claims()
    if generation:
        claims = claims.filter(claimed_generation=generation)
    return list(claims.order_by("pk").values_list("pk", flat=True))


def _close_admission(generation: str, *, timeout: int) -> None:
    if not generation:
        set_worker_quiescing(value=True)
        return

    from teatree.core.models import WorkerGeneration  # noqa: PLC0415 — deferred: ORM needs the app registry

    row = WorkerGeneration.objects.for_sha(generation).first()
    state = row.state if row is not None else "not registered"
    if state not in {WorkerGeneration.State.ACTIVE, WorkerGeneration.State.DRAINING}:
        msg = f"generation {short_sha(generation)} is {state} — only an active generation can drain"
        raise GenerationNotDrainableError(msg)
    row.begin_drain(deadline=timezone.now() + timedelta(seconds=timeout))


@dataclass(frozen=True, slots=True)
class DrainPacing:
    """How the wait polls — injectable so a test drives it without wall-clock time."""

    poll_interval: float = 5.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


_DEFAULT_PACING = DrainPacing()


def drain_worker(
    *,
    timeout: int,
    generation: str = "",
    pacing: DrainPacing = _DEFAULT_PACING,
    on_progress: Callable[[DrainProgress], None] | None = None,
) -> DrainReport:
    """Quiesce admission and wait for in-flight CLAIMED leases to clear.

    Sets ``worker_quiescing`` ON (so no new task is admitted), then polls the
    in-flight set every ``pacing.poll_interval`` seconds. Returns a :class:`DrainReport`
    with :attr:`DrainOutcome.DRAINED` as soon as no live lease remains, or
    :attr:`DrainOutcome.GRACE_EXCEEDED` (naming the still-CLAIMED pks) once
    ``timeout`` seconds elapse. The in-flight set is checked BEFORE the first
    sleep, so a quiet worker returns DRAINED immediately. ``on_progress`` receives
    one :class:`DrainProgress` per poll the wait continues past — the heartbeat the
    deploy's transport needs to tell a working drain from a hung session.

    A *generation* sha drains only that image generation: its registry row goes DRAINING
    (with ``timeout`` as its deadline) and only the claims it stamped are waited on, so the
    next generation keeps working and ``worker_quiescing`` is never written.
    """
    _close_admission(generation, timeout=timeout)
    start = pacing.monotonic()
    while True:
        still_claimed = _still_claimed_pks(generation)
        if not still_claimed:
            return DrainReport(outcome=DrainOutcome.DRAINED, waited_seconds=pacing.monotonic() - start)
        waited = pacing.monotonic() - start
        if waited >= timeout:
            return DrainReport(
                outcome=DrainOutcome.GRACE_EXCEEDED,
                waited_seconds=waited,
                still_claimed=still_claimed,
            )
        if on_progress is not None:
            on_progress(DrainProgress(waited_seconds=waited, still_claimed=still_claimed))
        pacing.sleep(pacing.poll_interval)


__all__ = [
    "QUIESCING_SETTING",
    "DrainOutcome",
    "DrainPacing",
    "DrainProgress",
    "DrainReport",
    "GenerationNotDrainableError",
    "drain_worker",
    "set_worker_quiescing",
]
