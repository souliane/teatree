from typing import TYPE_CHECKING

from django_fsm import TransitionNotAllowed

from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.types import validated_ticket_extra

if TYPE_CHECKING:
    from teatree.core.models.merge_void import MergeVoid
    from teatree.core.models.ticket import Ticket


class TicketStatusModel(TicketFacet):
    """The small direct-write status-flag mutations — remote-missing marking, unignore, merge void."""

    class Meta:
        abstract = True

    def mark_remote_missing(self) -> None:
        """Targeted UPDATE to set remote_missing; skips the FSM and save() overhead (#1875)."""
        type(self).objects.filter(pk=self.pk).update(remote_missing=True)
        self.remote_missing = True

    def unignore(self: "Ticket") -> None:
        if self.state != self.State.IGNORED:
            msg = f"Can't unignore from state '{self.state}'"
            raise TransitionNotAllowed(msg)
        extra = validated_ticket_extra(self.extra)
        previous = extra.pop("ignored_from", self.State.NOT_STARTED)
        self.extra = extra
        self.state = str(previous)

    def void_merge(self: "Ticket", void: "MergeVoid") -> str:
        """Not an FSM transition: those are offered on every generic transition surface."""
        if self.state not in self.merged_states():
            msg = f"Can't void a merge from state '{self.state}'"
            raise TransitionNotAllowed(msg)
        if void.pk is None or not void.tickets.filter(pk=self.pk).exists():
            msg = f"No recorded merge void names ticket {self.pk}"
            raise TransitionNotAllowed(msg)
        entry = self.transitions.merge_entry()
        if entry is None:
            msg = f"Ticket {self.pk} has no recorded edge into merged to restore"
            raise TransitionNotAllowed(msg)
        previous, self.state = self.state, entry.from_state
        self.transitions.create(
            session=self.sessions.order_by("-started_at").first(),
            from_state=previous,
            to_state=self.state,
            triggered_by="void_merge",
        )
        return self.state
