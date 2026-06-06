"""Durable away-mode question backlog (#58, BLUEPRINT §17.1 invariant 9 / §17.3 C3).

24/7 dual question-mode: when the resolved availability mode is ``away``,
the ``AskUserQuestion`` PreToolUse hook converts the tool call into a
:class:`DeferredQuestion` row instead of waiting on a TTY answer. The
question is *captured*, never silently dropped — exactly the §17.1
invariant 9 guarantee — and the user later answers it via
``t3 teatree questions list|answer|dismiss``.

This model mirrors the ``OnBehalfApproval`` shape (#960, mirrored from
#953 ``DbApproval`` and §17.4 ``MergeClear``):

* guarded factory :meth:`DeferredQuestion.record` is the only path that
    writes a row, and it refuses empty payloads (no silent drop);
* :meth:`consume` atomically claims and stamps ``answered_at``/
    ``dismissed_at`` so the same row can never be answered twice;
* :class:`DeferredQuestionAudit` is the post-answer audit row — who
    answered, what they answered, when — matching the
    ``MergeAudit``/``OnBehalfAudit``/``DbAudit`` family.

Unlike ``OnBehalfApproval`` (which records a *prior* approval the gate
*consumes*), a ``DeferredQuestion`` is a *queued question* the user
*resolves later*. The shape is identical — durable, single-use, scoped,
audited — so the team can reason about all four (DB, on-behalf, merge,
question) as the same primitive.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone


class DeferredQuestionError(ValueError):
    """A :class:`DeferredQuestion` was rejected at record time — contract failed."""


class DeferredQuestion(models.Model):
    """One queued user-directed question recorded while availability=away.

    The question text and options are the verbatim ``AskUserQuestion``
    payload; the hook layer (see ``hook_router.handle_route_away_mode_question``)
    is the only producer. Single-use: once :meth:`consume` stamps either
    ``answered_at`` or ``dismissed_at``, the row no longer matches a
    pending-question scan. The original ``tool_use_id`` (when the harness
    emits one) is stored verbatim so audits can be cross-referenced to
    the transcript.
    """

    STATUS_PENDING = "pending"
    STATUS_ANSWERED = "answered"
    STATUS_DISMISSED = "dismissed"

    question = models.TextField()
    options_json = models.TextField(blank=True, default="")
    session_id = models.CharField(max_length=255, blank=True, default="")
    tool_use_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    answered_at = models.DateTimeField(null=True, blank=True)
    answer_text = models.TextField(blank=True, default="")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "teatree_deferred_question"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"deferred-question<{self.pk}:{self.status} '{self.question[:40]}'>"

    @property
    def status(self) -> str:
        if self.answered_at is not None:
            return self.STATUS_ANSWERED
        if self.dismissed_at is not None:
            return self.STATUS_DISMISSED
        return self.STATUS_PENDING

    @property
    def is_pending(self) -> bool:
        return self.answered_at is None and self.dismissed_at is None

    @classmethod
    def record(
        cls,
        question: str,
        *,
        options_json: str = "",
        session_id: str = "",
        tool_use_id: str = "",
    ) -> "DeferredQuestion":
        """The single guarded factory for a queued question.

        Enforces the contract before any row is written and raises
        :class:`DeferredQuestionError` with a precise reason on the first
        violation: non-empty ``question`` after stripping. Construction is
        atomic so a rejected record leaves no partial row.
        """
        clean_question = question.strip()
        if not clean_question:
            msg = "question is required and must be non-empty (#58)"
            raise DeferredQuestionError(msg)

        with transaction.atomic():
            return cls.objects.create(
                question=clean_question,
                options_json=options_json or "",
                session_id=session_id or "",
                tool_use_id=tool_use_id or "",
            )

    @classmethod
    def pending(cls, *, using: str | None = None) -> models.QuerySet["DeferredQuestion"]:
        """Return the unanswered, undismissed queue, oldest first.

        The statusline and ``t3 teatree questions list`` use this — a row whose
        ``answered_at`` or ``dismissed_at`` is set is excluded.
        """
        manager = cls.objects.using(using) if using else cls.objects
        return manager.filter(answered_at__isnull=True, dismissed_at__isnull=True).order_by("created_at")

    @classmethod
    def consume(
        cls,
        question_id: int,
        *,
        answer: str = "",
        dismissed_reason: str = "",
        using: str | None = None,
    ) -> "DeferredQuestion | None":
        """Atomically resolve a pending question.

        Exactly one of ``answer`` / ``dismissed_reason`` must be non-empty
        — the caller chooses answer vs dismiss. Returns the consumed row
        (so the caller can write the audit) or ``None`` when the
        question is missing or already resolved. ``select_for_update``
        + ``answered_at``/``dismissed_at`` stamps make resolution
        single-use even under a concurrent second answer.
        """
        if bool(answer.strip()) == bool(dismissed_reason.strip()):
            msg = "consume requires exactly one of answer / dismissed_reason (#58)"
            raise DeferredQuestionError(msg)

        manager = cls.objects.using(using) if using else cls.objects
        with transaction.atomic(using=using):
            row = (
                manager.select_for_update()
                .filter(pk=question_id, answered_at__isnull=True, dismissed_at__isnull=True)
                .first()
            )
            if row is None:
                return None
            now = timezone.now()
            if answer.strip():
                row.answered_at = now
                row.answer_text = answer
                row.save(update_fields=["answered_at", "answer_text"], using=using)
            else:
                row.dismissed_at = now
                row.dismissed_reason = dismissed_reason
                row.save(update_fields=["dismissed_at", "dismissed_reason"], using=using)
            return row


class DeferredQuestionAudit(models.Model):
    """Post-resolution audit of a :class:`DeferredQuestion` (#58).

    Mirrors ``OnBehalfAudit`` / ``DbAudit`` / ``MergeAudit``: who resolved,
    what they answered (or why they dismissed), when. One row per
    resolution; the gate writes it inside the same atomic block as
    :meth:`DeferredQuestion.consume` so the resolution and the audit
    land together or not at all.
    """

    question = models.ForeignKey(
        DeferredQuestion,
        on_delete=models.CASCADE,
        related_name="audits",
    )
    action = models.CharField(max_length=16)  # "answered" | "dismissed"
    answer_text = models.TextField(blank=True, default="")
    dismissed_reason = models.TextField(blank=True, default="")
    resolver_id = models.CharField(max_length=255, blank=True, default="")
    resolved_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_deferred_question_audit"
        ordering: ClassVar = ["-resolved_at"]

    def __str__(self) -> str:
        return f"deferred-question-audit<{self.action}:{self.question.pk} by {self.resolver_id or '?'}>"
