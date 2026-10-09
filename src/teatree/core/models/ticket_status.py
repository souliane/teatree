from typing import TYPE_CHECKING

from django_fsm import TransitionNotAllowed

from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.types import validated_ticket_extra

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class TicketStatusModel(TicketFacet):
    """The small direct-write status-flag mutations — remote-missing marking and unignore."""

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
