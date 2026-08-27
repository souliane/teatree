"""Idempotency ledger + claimable-task factory for the headless directive interpreter (PR-6).

The mirror of :class:`~teatree.core.models.critic_dispatch.CriticDispatch` for the
interpret phase: when a ``CAPTURED`` (or re-dispatched ``CLARIFYING``) directive
needs interpreting, :meth:`enqueue` records a row keyed on
``(directive, purpose, generation)`` and creates the claimable headless
``Task(phase="directive_interpreting")`` the loop dispatches. The interpreter reads
the codebase and RETURNS a ``directive_interpretation`` envelope;
``attempt_recorder`` records the typed :class:`MechanismSketch` server-side
(maker≠checker — a different actor writes it than the one that captured the text).

Dedup is keyed on the LIVE interpreters of the directive's synthetic ticket, not on
this row's own task: answering a parked ``needs_user_input`` question fires
``schedule_resume``, a SECOND producer of ``directive_interpreting`` tasks
that owns no dispatch row, so a row-scoped predicate cannot see it and two claimable
interpreters end up live on one directive. The synthetic ticket is the per-directive
anchor both producers share, which makes it the liveness join key.

Once every interpreter is terminal WITHOUT an interpretation being recorded — the
governor refused it and the artifact sweep completed the still-PENDING task, or the
run failed the evidence gate — the directive is still awaiting interpretation, so a
re-tick RE-ARMS a fresh one on the same row rather than dedup-stranding the directive
in ``CAPTURED`` forever. That re-arm is rationed by :data:`MAX_INTERPRET_ATTEMPTS`:
an unbounded one retries hourly and forever on a directive no interpreter can read,
so an exhausted budget PARKS the directive with a reason instead. A clarification
bumps ``generation`` and arms its own fresh row. The row insert and the ``Task``
creation share one transaction so a row never exists without its task, and the whole
decision runs under a row lock on the DIRECTIVE — the anchor both predicates are
scoped to, and the only row that already exists on a first dispatch.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.directive import Directive
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.utils.url_slug import SYNTHETIC_LOOP_UMBRELLA_URL

INTERPRET_PHASE = "directive_interpreting"

#: How many interpreters the loop may arm for ONE directive before parking it.
#: A constant like its sibling ration ``MAX_OPEN_RATIFY_QUESTIONS``, not a setting:
#: nobody has asked to vary either per overlay. Five leaves room for a clarification
#: round-trip or two while bounding a directive no interpreter can read.
MAX_INTERPRET_ATTEMPTS = 5

#: The standing north-star self-modification umbrella the directive's synthetic interpret
#: ticket anchors under; the ``#directive=<pk>`` fragment makes each unique while still
#: resolving the ``souliane/teatree`` overlay via ``infer_overlay_for_url`` (the interpret
#: phase needs a ``Task``, and a ``Task`` needs a ``Ticket``). Single-sourced from
#: :data:`~teatree.utils.url_slug.SYNTHETIC_LOOP_UMBRELLA_URL` — the ONE anchor the task
#: sweep recognises so it never artifact-completes a synthetic loop ticket (#3706).
DIRECTIVE_UMBRELLA_URL = SYNTHETIC_LOOP_UMBRELLA_URL


def synthetic_interpret_ticket(directive: Directive) -> Ticket:
    """Get-or-create the synthetic ``Ticket`` the directive's interpret task anchors on.

    Idempotent per directive (the synthetic issue URL dedups), so a re-dispatch at
    a bumped generation reuses one ticket rather than accumulating rows.
    """
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=f"{DIRECTIVE_UMBRELLA_URL}#directive={directive.pk}",
        defaults={"role": Ticket.Role.AUTHOR, "short_description": f"interpret directive #{directive.pk}"},
    )
    return ticket


class DirectiveDispatch(models.Model):
    """One headless interpret dispatch for a directive generation — dedup key + task link."""

    class Purpose(models.TextChoices):
        INTERPRET = "interpret", "Interpret"

    directive = models.ForeignKey(
        "core.Directive",
        on_delete=models.CASCADE,
        related_name="dispatches",
    )
    purpose = models.CharField(max_length=32, choices=Purpose.choices, default=Purpose.INTERPRET)
    generation = models.PositiveIntegerField(default=0)
    task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="directive_dispatches",
    )
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_directive_dispatch"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["directive", "purpose", "generation"],
                name="uniq_directive_dispatch_directive_purpose_generation",
            ),
        ]

    if TYPE_CHECKING:
        # Django synthesises the ``<fk>_id`` shadow attribute at class-prep time —
        # invisible to a static checker. Declared here (annotation-only, never
        # evaluated at runtime) so ``__str__`` reads the id without a relation query.
        directive_id: int

    def __str__(self) -> str:
        return f"directive-dispatch<{self.pk}:directive:{self.directive_id} {self.purpose}@gen{self.generation}>"

    @staticmethod
    def _interpret_tasks(directive: Directive) -> "models.QuerySet[Task]":
        """Every interpret task ever anchored on *directive*'s synthetic ticket."""
        return Task.objects.filter(ticket=synthetic_interpret_ticket(directive), phase=INTERPRET_PHASE)

    @classmethod
    def live_interpreter_exists(cls, directive: Directive) -> bool:
        """Whether ANY interpreter is in flight for *directive* — dispatched or resumed.

        The dedup both producers are visible to: a resume task carries no dispatch row,
        so only the shared synthetic ticket sees it.
        """
        return cls._interpret_tasks(directive).filter(status__in=Task.Status.active()).exists()

    @classmethod
    def _attempts_exhausted(cls, directive: Directive) -> bool:
        """Whether the loop has already armed :data:`MAX_INTERPRET_ATTEMPTS` interpreters.

        Only dispatch-armed tasks count: a resume is the human's answer landing, not the
        loop retrying, and an engaged human is the opposite of the runaway this bounds.
        """
        return cls._interpret_tasks(directive).filter(parent_task__isnull=True).count() >= MAX_INTERPRET_ATTEMPTS

    @classmethod
    def enqueue(cls, *, directive: Directive, contract: str) -> "DirectiveDispatch | None":
        """Record the dispatch + create one claimable headless interpret task — idempotently.

        Returns the row (a fresh interpret task attached) on the first dispatch for
        ``(directive, interpret, generation)`` AND on a re-arm — when every prior
        interpreter is terminal without having recorded an interpretation. Returns
        ``None`` while any interpreter is still in flight, and ``None`` having PARKED
        the directive once :data:`MAX_INTERPRET_ATTEMPTS` are spent. Both decisions are
        taken while the directive row is LOCKED, so two concurrent ticks cannot both pass
        them and arm a second interpreter. The row insert and the ``Task`` creation share
        one transaction so a row never exists without its task.
        """
        with transaction.atomic():
            cls._lock_directive(directive)
            if cls.live_interpreter_exists(directive):
                return None
            if cls._attempts_exhausted(directive):
                directive.reject(reason=f"no interpretation recorded after {MAX_INTERPRET_ATTEMPTS} interpret attempts")
                return None
            row, _ = cls.objects.get_or_create(
                directive=directive,
                purpose=cls.Purpose.INTERPRET,
                generation=directive.generation,
            )
            row.task = cls._create_interpret_task(directive=directive, contract=contract)
            row.save(update_fields=["task"])
        return row

    @staticmethod
    def _lock_directive(directive: Directive) -> None:
        """Hold the per-directive row lock for the rest of the caller's transaction.

        The DIRECTIVE row, not this dispatch row: both predicates :meth:`enqueue`
        decides are directive-scoped — liveness spans generations through the shared
        synthetic ticket, and the budget counts every attempt — and on a first dispatch
        no dispatch row exists yet to lock. On the file-backed prod SQLite backend
        ``transaction_mode="IMMEDIATE"`` already serializes the whole block; the lock
        makes the decision correct on any backend.
        """
        Directive.objects.select_for_update().filter(pk=directive.pk).first()  # select-for-update: caller-atomic

    @staticmethod
    def _create_interpret_task(*, directive: Directive, contract: str) -> "Task":
        ticket = synthetic_interpret_ticket(directive)
        session = Session.objects.create(ticket=ticket, agent_id="directive-dispatch")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=INTERPRET_PHASE,
            execution_reason=contract,
        )
