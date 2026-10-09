"""Worker drain — quiesce admission, then wait for in-flight runs to checkpoint.

The first half of drain-then-deploy (rolling / zero-downtime deploy): a deploy
must never kill an in-flight sub-agent. ``drain_worker`` flips the
``worker_quiescing`` config gate ON — after which ``claim_admission_block_reason``
(the claim path) and ``headless_admission_block_reason`` (the auto-enqueue signal, the
queue drain and ``execute_task``) admit ZERO new work — then polls the live CLAIMED
leases a run is driving (``owner_driving_since``, or a claim too fresh to have marked
its drive yet) until none is left or the grace ``timeout`` lapses. An operator's
in-session claim is executed outside the worker, never checkpoints and survives the
swap, so it does not hold the drain. It NEVER stops the supervisor and never touches a
CLAIMED lease itself: each in-flight run reads the same gate at its next heartbeat
(``drain_block_reason``), interrupts itself and parks PENDING with its session id, so
the drain ends in about one heartbeat and the fresh worker resumes that conversation.

``deploy/deploy.sh`` runs ``t3 worker drain`` before swapping the worker image; the
FRESH worker's init clears ``worker_quiescing`` so admission resumes. On a grace
overrun (a run that could not checkpoint) the deploy proceeds anyway — a still-CLAIMED
task re-queues PENDING via its lease lapse (``reclaim_orphaned_claims``) and is picked
up by the fresh worker.

The wait reports a :class:`DrainProgress` sample on every poll. The deploy reaches this
command through an SSH session that tears down after ~280s of silence, so a wait that
says nothing cannot reach its own budget, and the deploy dies before the swap that
clears the gate (#3983).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TypedDict

from django.db.models import Q
from django.utils import timezone

from teatree.generation import short_sha

QUIESCING_SETTING = "worker_quiescing"

#: Ten heartbeats: a run checkpoints within one beat, or within the open-tool-call deferral cap plus the interrupt.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 600
#: How long a claim may sit before its run marks the drive; an operator's in-session claim never marks one.
_CLAIM_TO_DRIVE_GRACE = timedelta(seconds=60)


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


class QuiescePayload(TypedDict):
    since: str | None
    age_seconds: int | None
    in_flight: list[int]


@dataclass(frozen=True, slots=True)
class QuiesceStatus:
    """A worker held quiesced: since when (``None`` when no config row dates the gate) and what it waits on."""

    since: datetime | None
    age_seconds: float | None
    in_flight: list[int]

    def as_json(self) -> QuiescePayload:
        return {
            "since": self.since.isoformat() if self.since is not None else None,
            "age_seconds": round(self.age_seconds) if self.age_seconds is not None else None,
            "in_flight": self.in_flight,
        }

    def status_line(self) -> str:
        when = (
            f"since {self.since.astimezone(UTC):%H:%MZ} ({round(self.age_seconds or 0) // 60}m)"
            if self.since is not None
            else "outside the config store, so undateable"
        )
        pks = ", ".join(str(pk) for pk in self.in_flight)
        waiting = f"waiting on task(s) {pks}, which checkpoint at their next heartbeat" if pks else "no task in flight"
        return f"deploy drain: quiescing {when}, {waiting}"


def _quiescing_gate_set_at() -> datetime | None:
    """When the newest ON ``worker_quiescing`` row was written — ``None`` when env or file resolved it ON."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM needs the app registry

    return max(
        (
            row.updated_at
            for row in ConfigSetting.objects.filter(key=QUIESCING_SETTING)
            if row.value is True  # a JSONField holds any shape; only a literal ON dates the gate
        ),
        default=None,
    )


def quiesce_status() -> QuiesceStatus | None:
    """The quiesced worker's drain as an operator surface shows it, or ``None`` while admission is open."""
    from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 — deferred: heavy config import

    if not worker_is_quiescing():
        return None
    return _status_since(_quiescing_gate_set_at())


def stored_quiesce_status() -> QuiesceStatus | None:
    """The drain the stored gate row records: a render-time read that never resolves, nor marks, the config tier."""
    since = _quiescing_gate_set_at()
    return _status_since(since) if since is not None else None


def _status_since(since: datetime | None) -> QuiesceStatus:
    age = (timezone.now() - since).total_seconds() if since is not None else None
    return QuiesceStatus(since=since, age_seconds=age, in_flight=_still_claimed_pks(""))


class GenerationNotDrainableError(RuntimeError):
    """The named generation is not live, so there is nothing of its to drain."""


def _still_claimed_pks(generation: str) -> list[int]:
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM needs the app registry

    claims = Task.objects.active_claims().filter(
        Q(owner_driving_since__isnull=False) | Q(claimed_at__gte=timezone.now() - _CLAIM_TO_DRIVE_GRACE)
    )
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

    Sets ``worker_quiescing`` ON (so no new task is admitted and every in-flight run
    checkpoints at its next heartbeat), then polls the in-flight set every
    ``pacing.poll_interval`` seconds. Returns a :class:`DrainReport`
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
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "QUIESCING_SETTING",
    "DrainOutcome",
    "DrainPacing",
    "DrainProgress",
    "DrainReport",
    "GenerationNotDrainableError",
    "QuiescePayload",
    "QuiesceStatus",
    "drain_worker",
    "quiesce_status",
    "set_worker_quiescing",
    "stored_quiesce_status",
]
