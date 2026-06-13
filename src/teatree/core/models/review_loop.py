"""The bounded author<->reviewer ping-pong FSM (teatree#2298).

``ReviewLoop`` is an independent FSM — AUTHORING -> REVIEWING -> {PASSED,
EXHAUSTED} — that does NOT touch ``Ticket.State``. It encodes the iterate-
then-terminate review cycle the pre-#2298 ``review record`` chokepoint could
not: there, a HOLD verdict was inert (``_trigger_sweep`` early-returns on a
non-merge-safe verdict), so the punch-list never fed back to the author. Here
the ``hold`` transition re-arms a fresh author leg with the punch-list, bounded
by ``max_rounds``; when the rounds run out ``exhaust`` surfaces the loop for
human input instead of silently dropping it.

Every transition guard reads the *recorded verdict* (``recorded_kind``), never
the caller — so the verdict drives the FSM. Two variants:

*   ``SELF`` — the reviewer leg records an internal verdict on this row
    (``latest_verdict_kind`` / ``latest_findings``) and posts nothing. There is
    no ``ReviewVerdict`` and no egress, so ``_emit_review_done_signal`` /
    ``_trigger_sweep`` / the on-behalf egress have nothing to act on.
*   ``EXTERNAL`` — the reviewer leg records a real
    :class:`~teatree.core.models.review_verdict.ReviewVerdict` (bound here via
    ``latest_verdict``) through the normal cold-review path; ``pass_`` marks the
    proceed flag (``passed``) that the e2e evidence/gate flow consumes — it does
    NOT itself trigger a PR merge (that stays the ``review record`` sweep's job).

Leg dispatch is idempotent per ``(review_loop, round, leg)`` via the
:class:`ReviewLoopRound` slot, mirroring
:class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch`'s atomic
``get_or_create`` claim.
"""

from typing import TYPE_CHECKING, ClassVar, cast

from django.db import models, transaction
from django_fsm import FSMField, transition

if TYPE_CHECKING:
    from teatree.core.models.review_verdict import FindingDict, ReviewVerdict
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket


class ReviewLoopRound(models.Model):
    """The single-live-leg slot that makes leg dispatch idempotent.

    A unique ``(review_loop, round, leg)`` row is claimed before a leg's task
    is scheduled, so a re-fired scheduler for the same slot returns the
    existing leg instead of creating a duplicate task.
    """

    review_loop = models.ForeignKey("core.ReviewLoop", on_delete=models.CASCADE, related_name="round_slots")
    round = models.IntegerField()
    leg = models.CharField(max_length=16)
    task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "teatree_review_loop_round"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["review_loop", "round", "leg"],
                name="uniq_review_loop_round_leg",
            ),
        ]

    def __str__(self) -> str:
        return f"review-loop-round<{self.review_loop_id}:r{self.round}:{self.leg}>"  # type: ignore[attr-defined]


def _author_leg_done(loop: object) -> bool:
    from teatree.core.models.task import Task  # noqa: PLC0415

    instance = cast("ReviewLoop", loop)
    return instance.current_task is not None and instance.current_task.status == Task.Status.COMPLETED


def _is_merge_safe(loop: object) -> bool:
    return cast("ReviewLoop", loop).recorded_kind() == ReviewLoop.VerdictKind.MERGE_SAFE


def _is_hold(loop: object) -> bool:
    return cast("ReviewLoop", loop).recorded_kind() == ReviewLoop.VerdictKind.HOLD


def _has_rounds_left(loop: object) -> bool:
    instance = cast("ReviewLoop", loop)
    return instance.round + 1 < instance.max_rounds


def _rounds_exhausted(loop: object) -> bool:
    return not _has_rounds_left(loop)


class ReviewLoop(models.Model):
    class State(models.TextChoices):
        AUTHORING = "authoring", "Authoring"
        REVIEWING = "reviewing", "Reviewing"
        PASSED = "passed", "Passed"
        EXHAUSTED = "exhausted", "Exhausted"

    class Variant(models.TextChoices):
        EXTERNAL = "external", "External"
        SELF = "self", "Self"

    class VerdictKind(models.TextChoices):
        MERGE_SAFE = "merge_safe", "Merge-safe"
        HOLD = "hold", "Hold"

    LEG_AUTHOR: ClassVar[str] = "author"
    LEG_REVIEWER: ClassVar[str] = "reviewer"

    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="review_loops")
    variant = models.CharField(max_length=16, choices=Variant.choices)
    author_phase = models.CharField(max_length=64)
    reviewer_phase = models.CharField(max_length=64)
    state = FSMField(max_length=16, default=State.AUTHORING, choices=State.choices)
    round = models.IntegerField(default=0)
    max_rounds = models.IntegerField(default=3)
    latest_verdict = models.ForeignKey(
        "core.ReviewVerdict",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    latest_verdict_kind = models.CharField(max_length=16, choices=VerdictKind.choices, blank=True, default="")
    latest_findings = models.JSONField(default=list, blank=True)
    current_task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    passed = models.BooleanField(default=False)
    needs_user_input = models.BooleanField(default=False)
    user_input_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teatree_review_loop"
        ordering: ClassVar = ["-updated_at"]

    def __str__(self) -> str:
        return f"review-loop<{self.pk}:{self.variant}:{self.state}:r{self.round}>"

    @classmethod
    def start_self_loop(cls, *, ticket: "Ticket", max_rounds: int = 3) -> "ReviewLoop":
        return cls._start(
            ticket=ticket,
            variant=cls.Variant.SELF,
            author_phase="coding",
            reviewer_phase="reviewing",
            max_rounds=max_rounds,
        )

    @classmethod
    def start_external_loop(cls, *, ticket: "Ticket", max_rounds: int = 3) -> "ReviewLoop":
        return cls._start(
            ticket=ticket,
            variant=cls.Variant.EXTERNAL,
            author_phase="e2e",
            reviewer_phase="e2e_reviewing",
            max_rounds=max_rounds,
        )

    @classmethod
    def _start(
        cls,
        *,
        ticket: "Ticket",
        variant: str,
        author_phase: str,
        reviewer_phase: str,
        max_rounds: int,
    ) -> "ReviewLoop":
        loop = cls.objects.create(
            ticket=ticket,
            variant=variant,
            author_phase=author_phase,
            reviewer_phase=reviewer_phase,
            max_rounds=max_rounds,
        )
        loop._schedule_author_leg(findings=[])  # noqa: SLF001 — same-model first-leg arming.
        return loop

    @classmethod
    def open_external_for_ticket(cls, ticket_id: int) -> "ReviewLoop | None":
        """The single open EXTERNAL loop in REVIEWING for *ticket_id*, or ``None``.

        One open EXTERNAL loop per ticket is the norm (the round-leg slot keeps a
        single live leg); ``first()`` plus ``-updated_at`` ordering deterministically
        picks the most recent if more than one ever exists.
        """
        return cls.objects.filter(
            ticket_id=ticket_id,
            variant=cls.Variant.EXTERNAL,
            state=cls.State.REVIEWING,
        ).first()

    def advance_from_recorded_verdict(self, recorded: "ReviewVerdict") -> None:
        """Bind *recorded* and fire the verdict-guarded transition (#2298).

        MERGE_SAFE terminates at PASSED; a HOLD re-arms an author leg while rounds
        remain, else surfaces EXHAUSTED. The bind + transition + save are one
        atomic write so the loop never persists a verdict without its transition.
        """
        with transaction.atomic():
            self.latest_verdict = recorded
            if recorded.is_merge_safe():
                self.pass_()
            elif _has_rounds_left(self):
                self.hold()
            else:
                self.exhaust()
            self.save()

    def recorded_kind(self) -> str:
        if self.variant == self.Variant.EXTERNAL and self.latest_verdict is not None:
            return self.latest_verdict.verdict
        return self.latest_verdict_kind

    def _punch_list(self) -> list["FindingDict"]:
        if self.variant == self.Variant.EXTERNAL and self.latest_verdict is not None:
            return [finding.as_dict() for finding in self.latest_verdict.structured_findings]
        return list(self.latest_findings or [])

    @transition(field=state, source=State.AUTHORING, target=State.REVIEWING, conditions=[_author_leg_done])
    def submit_for_review(self) -> None:
        transaction.on_commit(self._schedule_reviewer_leg)

    @transition(
        field=state,
        source=State.REVIEWING,
        target=State.AUTHORING,
        conditions=[_is_hold, _has_rounds_left],
    )
    def hold(self) -> None:
        findings = self._punch_list()
        self.round += 1
        transaction.on_commit(lambda: self._schedule_author_leg(findings=findings))

    @transition(field=state, source=State.REVIEWING, target=State.PASSED, conditions=[_is_merge_safe])
    def pass_(self) -> None:
        if self.variant == self.Variant.EXTERNAL:
            self.passed = True

    @transition(
        field=state,
        source=State.REVIEWING,
        target=State.EXHAUSTED,
        conditions=[_is_hold, _rounds_exhausted],
    )
    def exhaust(self) -> None:
        self.needs_user_input = True
        self.user_input_reason = (
            f"review loop {self.pk} exhausted {self.max_rounds} rounds without a PASS — human input needed"
        )

    def _schedule_author_leg(self, *, findings: list["FindingDict"] | None = None) -> "Task":
        slot, created = self._claim_slot(self.LEG_AUTHOR)
        if not created and slot.task is not None:
            self.current_task = slot.task
            self.save(update_fields=["current_task", "updated_at"])
            return slot.task
        task = self._make_task(
            phase=self.author_phase,
            reason=self._author_reason(findings or []),
            leg=self.LEG_AUTHOR,
        )
        slot.task = task
        slot.save(update_fields=["task"])
        self.current_task = task
        self.save(update_fields=["current_task", "updated_at"])
        return task

    def _schedule_reviewer_leg(self) -> "Task":
        slot, created = self._claim_slot(self.LEG_REVIEWER)
        if not created and slot.task is not None:
            return slot.task
        task = self._make_task(
            phase=self.reviewer_phase,
            reason=self._reviewer_reason(),
            leg=self.LEG_REVIEWER,
        )
        slot.task = task
        slot.save(update_fields=["task"])
        return task

    def _claim_slot(self, leg: str) -> tuple[ReviewLoopRound, bool]:
        with transaction.atomic():
            return ReviewLoopRound.objects.get_or_create(review_loop=self, round=self.round, leg=leg)

    def _make_task(self, *, phase: str, reason: str, leg: str) -> "Task":
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self.ticket, agent_id=f"review-loop-{leg}")
        return Task.objects.create(
            ticket=self.ticket,
            session=session,
            phase=phase,
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason=reason,
        )

    def _author_reason(self, findings: list["FindingDict"]) -> str:
        if not findings:
            return f"Review-loop {self.pk} author leg (round {self.round}) — produce the work for review."
        lines = "; ".join(_finding_line(item) for item in findings)
        return (
            f"Review-loop {self.pk} author leg (round {self.round}) — address the reviewer punch-list "
            f"and resubmit: {lines}"
        )

    def _reviewer_reason(self) -> str:
        if self.variant == self.Variant.SELF:
            return (
                f"Review-loop {self.pk} reviewer leg (round {self.round}) — gate the author's work and "
                f"record the internal verdict on the ReviewLoop (latest_verdict_kind + latest_findings). "
                f"Findings feed straight back to the author; do NOT post and do NOT record a ReviewVerdict."
            )
        return (
            f"Review-loop {self.pk} reviewer leg (round {self.round}) — cold-review per /t3:review doctrine "
            f"and RECORD the verdict via `review record` bound to the reviewed head SHA."
        )


def _finding_line(item: "FindingDict") -> str:
    severity = str(item.get("severity") or "").strip()
    summary = str(item.get("summary") or "").strip()
    return f"[{severity}] {summary}" if severity else summary
