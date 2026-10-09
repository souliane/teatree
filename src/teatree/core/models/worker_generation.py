"""The registry of immutable code generations — which image's code is live, draining, or gone.

One row per commit sha a ``teatree-factory:<sha>`` image was rolled to. It is the single
source of truth the claim admission reads (a draining generation claims nothing new), the
per-generation drain waits on, and the roller advances; ``drain_deadline`` is the explicit
end of a drain that a host-side heuristic used to have to guess.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone
from django_fsm import FSMField, transition

from teatree.core.managers_task_claim import advance_quiesce_fence
from teatree.generation import generation_image, short_sha


class WorkerGenerationQuerySet(models.QuerySet["WorkerGeneration"]):
    STARTING_LEASE_MARGIN = timedelta(minutes=10)

    def for_sha(self, sha: str) -> "WorkerGenerationQuerySet":
        return self.filter(sha=sha)

    def live(self) -> "WorkerGenerationQuerySet":
        return self.exclude(state__in=[WorkerGeneration.State.RETIRED, WorkerGeneration.State.FAILED])

    def newest_first(self) -> "WorkerGenerationQuerySet":
        return self.order_by("-started_at", "-pk")

    def register(self, sha: str) -> "WorkerGeneration":
        """The row for *sha* a roll targets, STARTING: a failed, retired or draining one starts again."""
        with transaction.atomic():
            row, _ = self.get_or_create(sha=sha, defaults={"image": generation_image(sha)})
            if row.state == WorkerGeneration.State.DRAINING:
                row.retire()
            if row.state in WorkerGeneration.RESTARTABLE_STATES:
                row.retry()
        return row

    def reinstate(self, sha: str) -> None:
        """Bring *sha* back after a failed roll: a draining row resumes, one no longer live starts again."""
        with transaction.atomic():
            row, _ = self.get_or_create(sha=sha, defaults={"image": generation_image(sha)})
            if row.state == WorkerGeneration.State.DRAINING:
                row.resume()
            elif row.state in WorkerGeneration.RESTARTABLE_STATES:
                row.retry()

    def fail_displaced(self, *, serving: str) -> None:
        """Fail every active generation but *serving*: something other than the roller replaced its containers."""
        running = short_sha(serving) if serving else "the legacy stack"
        for row in self.filter(state=WorkerGeneration.State.ACTIVE).exclude(sha=serving):
            row.fail(reason=f"displaced outside the roller: the running worker is {running}")

    def boot(self, sha: str) -> "WorkerGeneration":
        """Activate a new or STARTING *sha*; only the roller may retry a FAILED one."""
        row, _ = self.get_or_create(sha=sha, defaults={"image": generation_image(sha)})
        if row.state == WorkerGeneration.State.STARTING:
            row.activate()
        return row

    def serving(self) -> "WorkerGeneration | None":
        """The newest ACTIVE generation, else the DRAINING one a killed roll stranded; ``None`` for a legacy stack."""
        live = self.filter(state__in=[WorkerGeneration.State.ACTIVE, WorkerGeneration.State.DRAINING])
        live = live.order_by("-activated_at", "-pk")
        return live.filter(state=WorkerGeneration.State.ACTIVE).first() or live.first()

    def state_of(self, sha: str) -> str:
        return self.for_sha(sha).values_list("state", flat=True).first() or ""

    def claim_refusing_state(self, sha: str) -> str:
        """*sha*'s state when it must claim nothing new, else ``""`` (an unregistered one still claims)."""
        state = self.state_of(sha)
        return state if state in WorkerGeneration.NON_CLAIMING_STATES else ""

    def reopen_stranded_drain(self, sha: str) -> bool:
        """Resume *sha* after its deadline when no successor serves or retains a starting lease."""
        # Every claim poll asks, so the common nothing-to-heal answer must not take the SQLite write lock.
        expired = self.for_sha(sha).filter(state=WorkerGeneration.State.DRAINING, drain_deadline__lt=timezone.now())
        if not expired.exists():
            return False
        with transaction.atomic():
            now = timezone.now()
            row = (
                self.select_for_update()
                .for_sha(sha)
                .filter(state=WorkerGeneration.State.DRAINING, drain_deadline__lt=now)
                .first()
            )
            if row is None:
                return False
            deadline = row.drain_deadline or now
            successors = list(
                self.select_for_update().exclude(pk=row.pk).filter(state__in=WorkerGeneration.SUCCESSOR_STATES)
            )
            if any(
                successor.state == WorkerGeneration.State.ACTIVE
                or now <= max(deadline, successor.started_at) + self.STARTING_LEASE_MARGIN
                for successor in successors
            ):
                return False
            for successor in successors:
                successor.fail(reason=f"starting lease expired while {short_sha(sha)} drained")
            row.resume()
        return True


WorkerGenerationManager = models.Manager.from_queryset(WorkerGenerationQuerySet)


class WorkerGeneration(models.Model):
    class State(models.TextChoices):
        STARTING = "starting", "Starting"
        ACTIVE = "active", "Active"
        DRAINING = "draining", "Draining"
        RETIRED = "retired", "Retired"
        FAILED = "failed", "Failed"

    sha = models.CharField(max_length=40, unique=True)
    image = models.CharField(max_length=255)
    state = FSMField(max_length=16, choices=State.choices, default=State.STARTING)
    started_at = models.DateTimeField(default=timezone.now)
    activated_at = models.DateTimeField(null=True, blank=True)
    drain_requested_at = models.DateTimeField(null=True, blank=True)
    drain_deadline = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")

    NON_CLAIMING_STATES: ClassVar[frozenset[str]] = frozenset({State.DRAINING, State.RETIRED, State.FAILED})
    SUCCESSOR_STATES: ClassVar[frozenset[str]] = frozenset({State.STARTING, State.ACTIVE})
    RESTARTABLE_STATES: ClassVar[frozenset[str]] = frozenset({State.FAILED, State.RETIRED})

    objects = WorkerGenerationManager()

    class Meta:
        app_label = "core"
        db_table = "teatree_worker_generation"

    def __str__(self) -> str:
        return f"{self.sha[:12]} ({self.state})"

    def activate(self) -> None:
        self._persist(self._to_active, "activated_at")

    def begin_drain(self, *, deadline: datetime) -> None:
        """Stop this generation claiming, fenced so a racing claim rolls back; a drain in progress is joined."""
        with self._locked() as state:
            if state == self.State.DRAINING:
                return
            self._to_draining(deadline)
            self.save(update_fields=["state", "drain_requested_at", "drain_deadline"])
            advance_quiesce_fence()

    def resume(self) -> None:
        self._persist(self._back_to_active, "drain_requested_at", "drain_deadline")

    def retire(self) -> None:
        self._persist(self._to_retired, "retired_at")

    def fail(self, *, reason: str) -> None:
        self._persist(lambda: self._to_failed(reason), "failure_reason")

    def retry(self) -> None:
        self._persist(
            self._to_starting, "failure_reason", "started_at", "retired_at", "drain_requested_at", "drain_deadline"
        )

    @contextmanager
    def _locked(self) -> Iterator[str]:
        with transaction.atomic():
            self.state = type(self).objects.select_for_update().values_list("state", flat=True).get(pk=self.pk)
            yield self.state

    def _persist(self, move: Callable[[], None], *fields: str) -> None:
        with self._locked():
            move()
            self.save(update_fields=["state", *fields])

    @transition(field="state", source=State.STARTING, target=State.ACTIVE)
    def _to_active(self) -> None:
        self.activated_at = timezone.now()

    @transition(field="state", source=State.ACTIVE, target=State.DRAINING)
    def _to_draining(self, deadline: datetime) -> None:
        self.drain_requested_at = timezone.now()
        self.drain_deadline = deadline

    @transition(field="state", source=State.DRAINING, target=State.ACTIVE)
    def _back_to_active(self) -> None:
        self.drain_requested_at = None
        self.drain_deadline = None

    @transition(field="state", source=State.DRAINING, target=State.RETIRED)
    def _to_retired(self) -> None:
        self.retired_at = timezone.now()

    @transition(field="state", source=[State.STARTING, State.ACTIVE], target=State.FAILED)
    def _to_failed(self, reason: str) -> None:
        self.failure_reason = reason

    @transition(field="state", source=[State.FAILED, State.RETIRED], target=State.STARTING)
    def _to_starting(self) -> None:
        self.failure_reason = ""
        self.started_at = timezone.now()
        self.retired_at = None
        self.drain_requested_at = None
        self.drain_deadline = None
