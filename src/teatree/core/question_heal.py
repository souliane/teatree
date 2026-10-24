"""Withdraw a pending question whose trigger has healed before anything surfaces it (#4904).

Each :data:`HEAL_CHECKS` entry maps a batch of pending rows to the ones it can PROVE
healed, with a reason. Positive-only: short of proof a row stays pending. The name is
the audit's resolver id, shared with the tick sweep in :mod:`teatree.loop.question_drain`.

A row whose text fails the plain-language checks is WITHHELD instead: moved off the owner
queue with an audit row, and asked again as a card under the same marker (#4990).
"""

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_withheld import WITHHELD_ACTION
from teatree.core.models.task_phase_disposition import phase_wedges_healed
from teatree.core.provision.failure_question import provision_failures_healed

if TYPE_CHECKING:
    from teatree.core.models.session import Session
    from teatree.core.models.task import Task

_WITHHELD_RESOLVER = "owner_card_check"

HealCheck = Callable[[Sequence[DeferredQuestion]], dict[int, str]]

HEAL_CHECKS: tuple[tuple[str, HealCheck], ...] = (
    ("provision_healed", provision_failures_healed),
    ("phase_wedge_healed", phase_wedges_healed),
)


def withdraw_healed(rows: Iterable[DeferredQuestion]) -> list[DeferredQuestion]:
    """Dismiss every healed row as stale and return the rest, in order."""
    live = list(rows)
    for name, check in HEAL_CHECKS:
        healed = check(live)
        for row in live:
            if (reason := healed.get(row.pk)) is not None:
                row.mark_stale(reason, resolver_id=name)
        live = [row for row in live if row.pk not in healed]
    return live


def live_owner_questions() -> list[DeferredQuestion]:
    """The pending owner-audience backlog, healed rows withdrawn."""
    return withdraw_healed(DeferredQuestion.owner_pending())


def withhold(row: DeferredQuestion, problems: Sequence[str]) -> bool:
    """Move a pending owner *row* to the internal queue, auditing *problems*; ``True`` when this call moved it."""
    marker = row.dedupe_marker or f"withheld:{row.pk}"
    with transaction.atomic():
        moved = DeferredQuestion.objects.filter(
            pk=row.pk,
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            answered_at__isnull=True,
            dismissed_at__isnull=True,
        ).update(audience=DeferredQuestion.Audience.INTERNAL, dedupe_marker=marker)
        if moved:
            DeferredQuestionAudit.objects.create(
                question=row, action=WITHHELD_ACTION, note="; ".join(problems), resolver_id=_WITHHELD_RESOLVER
            )
    if moved:
        row.audience, row.dedupe_marker = DeferredQuestion.Audience.INTERNAL, marker
    return bool(moved)


def record_withheld(
    question: str,
    problems: Sequence[str],
    *,
    parked_task: "Task | None",
    task_session: "Session | None",
    dedupe_marker: str,
) -> DeferredQuestion:
    """Record *question* as an internal row withheld for *problems* and parked on its task; once per marker."""
    with transaction.atomic():
        row = DeferredQuestion.record(
            question, parked_task=parked_task, task_session=task_session, dedupe_marker=dedupe_marker
        )
        if not DeferredQuestionAudit.objects.filter(question=row, action=WITHHELD_ACTION).exists():
            DeferredQuestionAudit.objects.create(
                question=row, action=WITHHELD_ACTION, note="; ".join(problems), resolver_id=_WITHHELD_RESOLVER
            )
    return row
