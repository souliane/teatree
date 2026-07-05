"""Idempotency ledger + claimable-task factory for the headless directive interpreter (PR-6).

The mirror of :class:`~teatree.core.models.critic_dispatch.CriticDispatch` for the
interpret phase: when a ``CAPTURED`` (or re-dispatched ``CLARIFYING``) directive
needs interpreting, :meth:`enqueue` records a row keyed on
``(directive, purpose, generation)`` and creates the claimable headless
``Task(phase="directive_interpreting")`` the loop dispatches. The interpreter reads
the codebase and RETURNS a ``directive_interpretation`` envelope;
``attempt_recorder`` records the typed :class:`MechanismSketch` server-side
(maker≠checker — a different actor writes it than the one that captured the text).

Dedup is per ``(directive, purpose, generation)``: a re-fire at the same
generation returns the existing row and enqueues no second interpreter; a
clarification bumps ``generation`` and arms exactly one fresh interpreter. The row
insert and the ``Task`` creation share one transaction so a row never exists
without its task.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.models.directive import Directive

INTERPRET_PHASE = "directive_interpreting"

#: The standing umbrella issue every directive's synthetic interpret ticket anchors
#: under; the ``#directive=<pk>`` fragment makes each unique while still resolving
#: the ``souliane/teatree`` overlay via ``infer_overlay_for_url`` (the outer-loop
#: synthetic-ticket idiom — the interpret phase needs a ``Task``, and a ``Task``
#: needs a ``Ticket``).
DIRECTIVE_UMBRELLA_URL = "https://github.com/souliane/teatree/issues/63"


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

    def __str__(self) -> str:
        return f"directive-dispatch<{self.pk}:directive:{self.directive_id} {self.purpose}@gen{self.generation}>"  # type: ignore[attr-defined]  # Django FK accessor

    @classmethod
    def enqueue(cls, *, directive: "Directive", contract: str) -> "DirectiveDispatch | None":
        """Record the dispatch + create one claimable headless interpret task — idempotently.

        Returns the new row on the first dispatch for ``(directive, interpret,
        generation)``; ``None`` when a row for that generation already exists (a
        prior tick already armed the interpreter). The row insert and the ``Task``
        creation share one transaction so a row never exists without its task.
        """
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                directive=directive,
                purpose=cls.Purpose.INTERPRET,
                generation=directive.generation,
            )
            if not created:
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
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason=contract,
        )
