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

import hashlib
import re
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.task import Task

_WHITESPACE_RE = re.compile(r"\s+")


def question_fingerprint(text: str) -> str:
    """A normalized-text fingerprint that collapses cosmetically-different clones.

    Lowercases, strips, and collapses runs of whitespace before hashing, so eight
    "I lack the tools to review" review-failure clones — differing only in
    trailing whitespace or casing — map to one marker and dedup to a single
    :class:`DeferredQuestion` instead of eight identical rows.
    """
    normalized = _WHITESPACE_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


#: Signals that a ``needs_user_input`` reason is a tool-lack / mis-provisioned
#: DISPATCH fault — a session reporting it lacks the tools / checkout / access to do
#: its assigned work — not a genuine decision the owner must make. Any one branch is
#: sufficient. Each keys on a signal owner *decision* questions do not carry, so a
#: real "how should I proceed on X?" ("cannot decide", "no clean approach") stays
#: OWNER_QUESTION. Branches (4)-(7) were added after (1)-(3) still leaked review
#: parks that reported the same fault by its consequence/symptom (#201/#202).
_TOOL_LACK_SELFREPORT_RE = re.compile(
    r"(?:"
    # (1) capability negation adjacent to a tool word
    r"\b(?:lack|lacks|lacking|no|without|missing|denied|deprived of)\b[^.]{0,40}?"
    r"\b(?:shell|bash|gh|tool|tools|toolset)\b"
    r"|\bshell[- ]?denied\b"  # (2) bare "shell-denied"
    r"|\bneeds?\b[^.]{0,40}?\bsession\b[^.]{0,40}?\btool"  # (3) hand-off phrasings
    r"|\bsession with (?:the )?(?:standard )?tool"
    r"|\bpicked up by (?:a )?session\b"
    # (4) dispatch-provisioning phrase ("tool access") — only in a provisioning report
    r"|\btool access\b"
    # (5) no accessible checkout / working tree / working copy / repo access
    r"|\bno\b[^.]{0,30}?\b(?:accessible )?(?:checkout|working tree|working copy|repo(?:sitory)? access)\b"
    # (6) internal task-context tools (TaskGet/TaskList/TaskRead) returning nothing
    r"|\btask(?:get|list|read)\b[^.]{0,60}?\b(?:returned nothing|nothing|empty|unavailable|no rows)\b"
    # (7) inability to do tool-requiring work (the consequence phrasing of a lack)
    r"|\b(?:cannot|can't|can not|unable to|couldn't|could not)\b[^.]{0,60}?"
    r"\b(?:inspect|make code changes|run the required|run [^.]{0,20}?verify-gates|verify-gates"
    r"|clone|check ?out|apply the patch)\b"
    r")",
    re.IGNORECASE,
)


def is_tool_lack_selfreport(text: str) -> bool:
    """True if *text* is an agent's own "I lack the tools to proceed" dispatch fault.

    An agent that stops with ``needs_user_input`` because its session was
    dispatched WITHOUT the shell / ``gh`` / toolset / checkout its own work needs is
    reporting a DISPATCH fault — a phase mis-provisioned for its job — not asking the
    owner to decide anything. Surfacing that self-report to the owner's DM is the
    exact leak this classifier defends (it reached the owner as "*Pending question* …
    This session lacks any shell/write tool …", and later as the review-phase
    "launched without … tool access, so I cannot inspect the PR diff …" / "no shell,
    TaskGet/TaskList returned nothing" leaks). Such a reason is recorded ``INTERNAL``
    — logged / statusline-only, never DM'd. See ``_TOOL_LACK_SELFREPORT_RE``.
    """
    return bool(_TOOL_LACK_SELFREPORT_RE.search(_WHITESPACE_RE.sub(" ", text.strip())))


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

    class ResolvedVia(models.TextChoices):
        UNRESOLVED = "", "Unresolved"
        SLACK = "slack", "Slack reply"
        LOCAL = "local", "Local CLI"
        STALE = "stale", "Stale"
        POLICY = "policy", "Policy auto-answer"  # #119 graduation: the dial answered, not a human

    class Audience(models.TextChoices):
        OWNER_QUESTION = "owner_question", "Owner question"
        INTERNAL = "internal", "Internal"

    question = models.TextField()
    # Who the question is for. OWNER_QUESTION rows are DM'd to the owner; INTERNAL
    # rows (repair-loop stalls, dispatch-health escalations synthesized by the box
    # about its OWN health) are logged/statusline-only and never reach the owner
    # feed — mirroring ``NotifyAudience`` so the two queues share one audience model.
    audience = models.CharField(
        max_length=16,
        default=Audience.OWNER_QUESTION,
        choices=Audience.choices,
        db_index=True,
    )
    options_json = models.TextField(blank=True, default="")
    session_id = models.CharField(max_length=255, blank=True, default="")
    tool_use_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    answered_at = models.DateTimeField(null=True, blank=True)
    answer_text = models.TextField(blank=True, default="")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True, default="")
    slack_ts = models.CharField(max_length=64, blank=True, default="")
    slack_channel = models.CharField(max_length=64, blank=True, default="")
    options_hash = models.CharField(max_length=64, blank=True, default="")
    # Idempotency marker for escalation-once callers (repair-loop stalls, headless
    # needs-input parks): a non-empty marker collapses repeat records of the same
    # underlying signal to one PENDING row. Blank for the ordinary capture path.
    dedupe_marker = models.CharField(max_length=64, blank=True, default="", db_index=True)
    generation = models.PositiveIntegerField(default=0)
    run_id = models.CharField(max_length=255, blank=True, default="")
    parked_task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deferred_questions",
    )
    resolved_via = models.CharField(
        max_length=8,
        blank=True,
        default="",
        choices=ResolvedVia.choices,
    )
    applied_at = models.DateTimeField(null=True, blank=True)

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

    @property
    def stable_notify_ref(self) -> str:
        """Stable discriminator for outward-notification idempotency keys.

        Prefers the harness-assigned ``tool_use_id`` — stable across restarts
        and independent of this DB's autoincrement — so a key built from it does
        not shift when the local pk does. Only when the harness supplied no
        ``tool_use_id`` does it fall back to the pk, qualified by the fleet
        ``instance_id`` (:mod:`teatree.instance_id`) so two instances'
        independently-numbered rows can never collide into a false-dedup on a
        shared operator DM surface. Never the bare local pk.
        """
        if self.tool_use_id:
            return self.tool_use_id
        from teatree.instance_id import instance_id  # noqa: PLC0415 — leaf import kept out of module load

        return f"{instance_id()}:{self.pk}"

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — guarded factory: each kwarg is a documented column, kwargs-only.
        cls,
        question: str,
        *,
        options_json: str = "",
        session_id: str = "",
        tool_use_id: str = "",
        slack_ts: str = "",
        slack_channel: str = "",
        options_hash: str = "",
        generation: int = 0,
        run_id: str = "",
        dedupe_marker: str = "",
        parked_task: "Task | None" = None,
        audience: str = Audience.OWNER_QUESTION,
    ) -> "DeferredQuestion":
        """The single guarded factory for a queued question.

        Enforces the contract before any row is written and raises
        :class:`DeferredQuestionError` with a precise reason on the first
        violation: non-empty ``question`` after stripping. Construction is
        atomic so a rejected record leaves no partial row. The mirror
        kwargs (``slack_ts`` / ``slack_channel`` / ``options_hash`` /
        ``generation`` / ``run_id``) link the row to its Slack DM so a
        later Slack reply can resolve exactly the live generation (#1174).
        ``parked_task`` correlates a headless-lane question back to the SDK
        task that emitted ``needs_user_input`` so the reply re-queues a
        headless resume of that task (the SDK lane has no Slack DM yet — the
        tick-level poster scanner mirrors it later).

        ``dedupe_marker`` is the escalate-once guard: when non-empty, an existing
        PENDING row already carrying that marker is returned instead of writing a
        duplicate — so two consecutive repair-loop stalls, or eight identical
        "I lack tools" review-failure parks, collapse to a single queued question
        rather than flooding the backlog.
        """
        clean_question = question.strip()
        if not clean_question:
            msg = "question is required and must be non-empty (#58)"
            raise DeferredQuestionError(msg)

        with transaction.atomic():
            if dedupe_marker:
                existing = (
                    cls.objects.select_for_update()
                    .filter(dedupe_marker=dedupe_marker, answered_at__isnull=True, dismissed_at__isnull=True)
                    .first()
                )
                if existing is not None:
                    return existing
            return cls.objects.create(
                question=clean_question,
                options_json=options_json or "",
                session_id=session_id or "",
                tool_use_id=tool_use_id or "",
                slack_ts=slack_ts or "",
                slack_channel=slack_channel or "",
                options_hash=options_hash or "",
                generation=generation,
                run_id=run_id or "",
                dedupe_marker=dedupe_marker or "",
                parked_task=parked_task,
                audience=audience or cls.Audience.OWNER_QUESTION,
            )

    @classmethod
    def unmirrored_pending(cls) -> models.QuerySet["DeferredQuestion"]:
        """Pending rows with no Slack mirror yet, oldest first.

        The headless lane and ``task_repair._escalate_stall`` record a row
        with an empty ``slack_ts``; the tick-level poster drains exactly these
        so a reply can later bind. A row already mirrored (``slack_ts != ""``)
        or resolved is excluded.
        """
        return cls.objects.filter(
            answered_at__isnull=True,
            dismissed_at__isnull=True,
            slack_ts="",
            audience=cls.Audience.OWNER_QUESTION,
        ).order_by("created_at")

    def mark_mirrored(self, *, channel: str, slack_ts: str) -> bool:
        """Stamp the Slack mirror coordinates single-use; ``True`` on the transition.

        An idempotent ``UPDATE … WHERE slack_ts = ''`` so a concurrent second
        drain (or a re-tick after a partial stamp) sees 0 rows updated and does
        not re-stamp — the verify-by-re-read seam for the poster scanner.
        """
        if not channel or not slack_ts:
            return False
        updated = bool(
            type(self).objects.filter(pk=self.pk, slack_ts="").update(slack_ts=slack_ts, slack_channel=channel)
        )
        if updated:
            self.slack_ts = slack_ts
            self.slack_channel = channel
        return updated

    @classmethod
    def next_generation(cls, *, session_id: str, run_id: str) -> int:
        """The next per-(session, run) question cursor — ``max(generation) + 1``.

        A Slack reply resolves only the current generation, so each new
        captured question for a (session, run) scope claims a strictly
        higher cursor. Atomic max-then-increment under the row lock the
        caller already holds when superseding the prior generation.
        """
        current = cls.objects.filter(session_id=session_id, run_id=run_id).aggregate(top=Max("generation"))["top"]
        return (current or 0) + 1

    @classmethod
    def live_for_reply(cls, *, channel: str, after_ts: str) -> "DeferredQuestion | None":
        """The single currently-live question a Slack reply can resolve.

        Returns the highest-generation pending row mirrored to *channel*
        whose mirror ``slack_ts`` is strictly before *after_ts* (the
        reply's ts) — so a reply can never bind a question posted after it
        (the ``after_ts`` guard). ``None`` when no such live row exists,
        which the caller treats as a stale reply (ordinary DM context).
        """
        if not channel or not after_ts:
            return None
        return (
            cls.objects.filter(
                slack_channel=channel,
                slack_ts__lt=after_ts,
                slack_ts__gt="",
                answered_at__isnull=True,
                dismissed_at__isnull=True,
            )
            .order_by("-generation", "-created_at")
            .first()
        )

    def mark_stale(self, reason: str) -> None:
        """Stamp ``dismissed_at`` + ``resolved_via='stale'`` + audit, single-use.

        Used at capture-time supersession (a newer-generation question
        arrived) and as the terminal state for a reply that found no live
        row. A no-op on an already-resolved row.
        """
        with transaction.atomic():
            row = (
                type(self)
                .objects.select_for_update()
                .filter(pk=self.pk, answered_at__isnull=True, dismissed_at__isnull=True)
                .first()
            )
            if row is None:
                return
            row.dismissed_at = timezone.now()
            row.dismissed_reason = reason
            row.resolved_via = self.ResolvedVia.STALE
            row.save(update_fields=["dismissed_at", "dismissed_reason", "resolved_via"])
            DeferredQuestionAudit.objects.create(
                question=row,
                action="dismissed",
                dismissed_reason=reason,
            )
            self.dismissed_at = row.dismissed_at
            self.dismissed_reason = row.dismissed_reason
            self.resolved_via = row.resolved_via

    def apply_answer(self, answer: str, *, resolved_via: str) -> "DeferredQuestion | None":
        """Resolve this pending row with *answer*, stamping ``resolved_via``.

        Wraps :meth:`consume` (the single-use CAS that stamps
        ``answered_at`` + ``answer_text``) and additionally records
        ``resolved_via``. Returns the consumed row, or ``None`` when the
        row was already resolved (a concurrent answer won). ``applied_at``
        is stamped separately by the UserPromptSubmit drain — this marks
        the answer *recorded*, not yet *delivered* back to a session.
        """
        with transaction.atomic():
            row = type(self).consume(self.pk, answer=answer)
            if row is None:
                return None
            row.resolved_via = resolved_via
            row.save(update_fields=["resolved_via"])
            return row

    @classmethod
    def answered_not_applied(cls, *, session_id: str = "") -> models.QuerySet["DeferredQuestion"]:
        """Rows answered but whose answer has not yet been delivered to a session.

        The UserPromptSubmit drain reads these to emit each resolved
        answer into ``additionalContext`` exactly once. Scoped to
        *session_id* when given (an empty session matches every row, the
        v1 single-session path).
        """
        qs = cls.objects.filter(answered_at__isnull=False, applied_at__isnull=True).order_by("created_at")
        if session_id:
            qs = qs.filter(session_id=session_id)
        return qs

    @classmethod
    def mark_applied(cls, question_id: int) -> bool:
        """Stamp ``applied_at`` single-use; ``True`` on the transition, else ``False``.

        The at-most-once gate for delivering an answer back into a
        session's ``additionalContext`` (``UPDATE … WHERE applied_at IS
        NULL``): a concurrent second drain sees 0 rows updated and emits
        nothing.
        """
        return bool(cls.objects.filter(pk=question_id, applied_at__isnull=True).update(applied_at=timezone.now()))

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
