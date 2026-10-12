from typing import TYPE_CHECKING

from django_fsm import RETURN_VALUE, TransitionNotAllowed, transition

from teatree.core.models.merge_void import ManualVoidPermit
from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.types import validated_ticket_extra

VOID_MERGE_TARGET_STATES = (
    "not_started",
    "scoped",
    "work_started",
    "plan_recorded",
    "coded",
    "tested",
    "self_reviewed",
    "pr_opened",
    "review_requested",
)

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class TicketStatusModel(TicketFacet):
    """The small direct-write status-flag mutations — remote-missing marking and unignore."""

    class Meta:
        abstract = True

    @transition(field="state", source="merged", target=RETURN_VALUE(*VOID_MERGE_TARGET_STATES))
    def void_merge(self: "Ticket", previous_state: str, *, permit: ManualVoidPermit) -> str:
        if previous_state not in self._MERGED_RECONCILE_SOURCE_STATES or previous_state == self.State.MERGED:
            msg = f"Invalid pre-merge ticket state: {previous_state}"
            raise ValueError(msg)
        permit.require_absent()
        return previous_state

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
