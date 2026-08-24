"""Idempotency ledger + claimable-task factory for the headless directive interpreter (PR-6).

The mirror of :class:`~teatree.core.models.critic_dispatch.CriticDispatch` for the
interpret phase: when a ``CAPTURED`` (or re-dispatched ``CLARIFYING``) directive
needs interpreting, :meth:`enqueue` records a row keyed on
``(directive, purpose, generation)`` and creates the claimable headless
``Task(phase="directive_interpreting")`` the loop dispatches. The interpreter reads
the codebase and RETURNS a ``directive_interpretation`` envelope;
``attempt_recorder`` records the typed :class:`MechanismSketch` server-side
(maker≠checker — a different actor writes it than the one that captured the text).

Dedup is per ``(directive, purpose, generation)`` but keyed on the LIVE task, not
the row: a re-fire while the generation's interpret task is still in flight
(PENDING/CLAIMED) returns the existing row and enqueues no second interpreter. Once
that task reaches a terminal status WITHOUT an interpretation being recorded — the
governor refused it and the artifact sweep completed the still-PENDING task, or the
run failed the evidence gate — the directive is still awaiting interpretation, so a
re-tick RE-ARMS a fresh interpreter on the same row rather than dedup-stranding the
directive in ``CAPTURED`` forever. A clarification bumps ``generation`` and arms its
own fresh row. The row insert and the ``Task`` creation share one transaction so a
row never exists without its task.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.utils.url_slug import SYNTHETIC_LOOP_UMBRELLA_URL

if TYPE_CHECKING:
    from teatree.core.models.directive import Directive

INTERPRET_PHASE = "directive_interpreting"

#: The standing north-star self-modification umbrella the directive's synthetic interpret
#: ticket anchors under; the ``#directive=<pk>`` fragment makes each unique while still
#: resolving the ``souliane/teatree`` overlay via ``infer_overlay_for_url`` (the interpret
#: phase needs a ``Task``, and a ``Task`` needs a ``Ticket``). Single-sourced from
#: :data:`~teatree.utils.url_slug.SYNTHETIC_LOOP_UMBRELLA_URL` — the ONE anchor the task
#: sweep recognises so it never artifact-completes a synthetic loop ticket (#3706).
DIRECTIVE_UMBRELLA_URL = SYNTHETIC_LOOP_UMBRELLA_URL


def synthetic_interpret_ticket(directive: "Directive") -> Ticket:
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

    def has_live_interpreter(self) -> bool:
        """Whether this dispatch's interpret task is still in flight (PENDING/CLAIMED).

        A live task means an interpreter is already armed for this generation — a
        re-tick waits on it (the dedup). A terminal or absent task means the prior
        interpreter finished without an interpretation being recorded, so the
        directive is still awaiting one and a re-tick may RE-ARM a fresh interpreter.
        """
        return self.task is not None and self.task.status in Task.Status.active()

    @classmethod
    def enqueue(cls, *, directive: "Directive", contract: str) -> "DirectiveDispatch | None":
        """Record the dispatch + create one claimable headless interpret task — idempotently.

        Returns the row (a fresh interpret task attached) on the first dispatch for
        ``(directive, interpret, generation)`` AND on a re-arm — when a row for that
        generation already exists but its interpreter is terminal without having
        recorded an interpretation. Returns ``None`` only while the generation's
        interpreter is still in flight (a live PENDING/CLAIMED task). The row insert
        and the ``Task`` creation share one transaction so a row never exists without
        its task.
        """
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                directive=directive,
                purpose=cls.Purpose.INTERPRET,
                generation=directive.generation,
            )
            if not created:
                # Lock the existing row so two concurrent ticks cannot both pass the
                # terminal-task check and double-arm two live interpreters. On the
                # file-backed prod SQLite backend the atomic already serializes writers;
                # the row lock makes it correct on any backend if the loop ever fans out.
                row = cls.objects.select_for_update().get(pk=row.pk)
                if row.has_live_interpreter():
                    return None
            row.task = cls._create_interpret_task(directive=directive, contract=contract)
            row.save(update_fields=["task"])
        return row

    @staticmethod
    def _create_interpret_task(*, directive: "Directive", contract: str) -> "Task":
        ticket = synthetic_interpret_ticket(directive)
        session = Session.objects.create(ticket=ticket, agent_id="directive-dispatch")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=INTERPRET_PHASE,
            execution_reason=contract,
        )
