"""Close the board's merge-refusal issue on the save that LANDS a ticket through the FSM, and on no other save.

The refusal is pinned against auto-resolve, so a landing is the only thing that clears it. The transition
into a merged state only marks the instance; the ``post_save`` that persists that state settles the issue
and clears the mark. Resolving at the transition would close it for a landing whose save then fails, and
resolving on every save of a landed ticket would take the production SQLite write lock a second time for
an unrelated ``extra`` write. A save that does not write ``state`` keeps the mark for the save that does.

The tracker syncs land tickets through a bulk ``merge_extra(also_set=...)`` that fires neither signal;
``merge_extra`` settles the refusal itself on that path.
"""

from django.db.models.signals import post_save
from django_fsm.signals import post_transition

from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.ticket import Ticket

#: The instance attribute a landing transition sets and the save persisting it clears.
_LANDING_MARK = "_teatree_landing_unsaved"


def _mark_a_landing(*, instance: Ticket, source: str, target: str, **_kwargs: object) -> None:
    # self-loop-safe: a self-loop into a landed state starts in one, and `source not in landed` never marks it.
    landed = Ticket.merged_states()
    if target in landed and source not in landed:
        instance.__dict__[_LANDING_MARK] = True


def _settle_on_the_landing_save(*, instance: Ticket, update_fields: frozenset[str] | None, **_kwargs: object) -> None:
    if (update_fields is None or "state" in update_fields) and instance.__dict__.pop(_LANDING_MARK, False):
        KnownIssue.objects.settle_merge_refusal(int(instance.pk))


def connect_merge_refusal_settlement() -> None:
    post_transition.connect(_mark_a_landing, sender=Ticket, dispatch_uid="ticket_landing_marks_merge_refusal")
    post_save.connect(_settle_on_the_landing_save, sender=Ticket, dispatch_uid="ticket_merged_resolves_refusal")
