"""Durable session/task-scoped honesty-critical escalation signal (#2263).

A row of this model marks that the next verification/review/grading spawn for a
session should route to the most-honest model (today Fable, config-driven via
``[agent] honesty_model``). It is *situational* — not a standing phase floor —
and *auto-clearing*: a row is active only while it is uncleared AND inside its
TTL window, so a forgotten row never pins Fable forever.

Four triggers write a row (the firing is mostly agent-judgment, the consequence
is deterministic):

- ``user_asked`` — the user explicitly asks the agent to be honest.
- ``self_assessed_dishonest`` — the agent judges it has been dishonest.
- ``accused_of_lying`` — the user accuses the agent of lying / "successfully
    failing" a task.
- ``shipped_incomplete`` — the agent shipped a job it cannot verify is complete
    (the rubric done-gate refusal is the one deterministic backstop).

The PRIMARY clear is :meth:`mark_cleared`, called when an honest,
verified-complete landing happens (the rubric-grade fully-passed success path).
:attr:`expires_at` is a 6-hour safety-net backstop (:data:`_DEFAULT_TTL`), not
the main mechanism — it bounds a row the explicit clear never reached.
:meth:`is_active` filters ``cleared_at IS NULL AND expires_at > now()``, so the
TTL is a read-time auto-clear with no cron.

Mirrors :class:`teatree.core.models.red_card_signal.RedCardSignal` in shape: one
model per module, an idempotent :meth:`record` classmethod keyed on the logical
identity, and short state-advancing methods.
"""

from datetime import timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone

# Safety-net TTL: a forgotten (never-:meth:`mark_cleared`) row stops escalating
# after this window. The PRIMARY clear is :meth:`mark_cleared` on an honest,
# verified-complete landing — this only bounds a row that clear never reached.
# A module constant so it is tunable in one place; deliberately generous so a
# legitimate in-flight escalation is not dropped mid-session.
_DEFAULT_TTL = timedelta(hours=6)


class HonestyEscalation(models.Model):
    """One situational honesty-critical escalation for a session/task (#2263)."""

    class Reason(models.TextChoices):
        USER_ASKED = "user_asked", "User explicitly asked for honesty"
        SELF_ASSESSED_DISHONEST = "self_assessed_dishonest", "Agent judges it was dishonest"
        ACCUSED_OF_LYING = "accused_of_lying", "User accused the agent of lying"
        SHIPPED_INCOMPLETE = "shipped_incomplete", "Shipped a job not verified complete"

    session_id = models.CharField(max_length=255)
    task_id = models.IntegerField(null=True, blank=True)
    reason = models.CharField(max_length=32, choices=Reason.choices)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_honesty_escalation"
        ordering: ClassVar = ["created_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["session_id", "task_id", "reason"],
                name="uniq_honestyescalation_session_task_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"honesty<{self.pk}:{self.reason} session={self.session_id} task={self.task_id}>"

    @classmethod
    def record(
        cls,
        reason: "str | HonestyEscalation.Reason",
        *,
        session_id: str,
        task_id: int | None = None,
        ttl: timedelta | None = None,
    ) -> "HonestyEscalation | None":
        """Insert one escalation row idempotently on ``(session_id, task_id, reason)``.

        Returns the row on first observation, ``None`` when an identical row
        already exists (the caller treats ``None`` as "already escalated — skip"
        so re-firing the same trigger never duplicates). A blank ``session_id``
        is refused (``None``) — escalation is session-scoped and a row with no
        session can never be matched by :meth:`is_active`. ``expires_at`` is set
        to ``now + ttl`` (default :data:`_DEFAULT_TTL`, the 6h safety net).
        """
        if not session_id:
            return None
        now = timezone.now()
        window = ttl if ttl is not None else _DEFAULT_TTL
        row, created = cls.objects.get_or_create(
            session_id=session_id,
            task_id=task_id,
            reason=str(reason),
            defaults={"created_at": now, "expires_at": now + window},
        )
        return row if created else None

    @classmethod
    def is_active(cls, session_id: str, *, task_id: int | None = None) -> bool:
        """Whether an active escalation exists for *session_id* (optionally scoped to *task_id*).

        Active means a row that is uncleared (``cleared_at IS NULL``) AND inside
        its TTL window (``expires_at > now()``) — the ``expires_at`` predicate is
        the read-time auto-clear (no cron). A blank *session_id* is never active.

        A ticket-wide row (``task_id IS NULL``) always matches the session; a
        task-scoped row matches only when *task_id* equals its own. So an unscoped
        query (``task_id=None``) sees only the session-wide rows, and a
        task-scoped query sees those PLUS its own task's rows — a task-specific
        escalation never bleeds into a sibling task's spawns.
        """
        if not session_id:
            return False
        scope = models.Q(task_id__isnull=True)
        if task_id is not None:
            scope |= models.Q(task_id=task_id)
        return cls.objects.filter(
            scope,
            session_id=session_id,
            cleared_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exists()

    @classmethod
    def mark_cleared(cls, session_id: str) -> int:
        """Clear every active escalation for *session_id* — the PRIMARY clear.

        Called when an honest, verified-complete landing happens (the
        rubric-grade fully-passed success path). Stamps ``cleared_at`` on every
        currently-active row for the session and returns the number cleared
        (``0`` when none were active — idempotent, so a second call is a no-op).
        A blank *session_id* clears nothing. Session-wide like :meth:`is_active`.
        """
        if not session_id:
            return 0
        return cls.objects.filter(session_id=session_id, cleared_at__isnull=True).update(cleared_at=timezone.now())
