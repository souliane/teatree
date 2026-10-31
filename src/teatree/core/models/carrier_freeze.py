"""A declared merge round freezes its member tickets while the carrier PR is open."""

from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models.pull_request import PullRequest
    from teatree.core.models.ticket import Ticket

FROZEN_PHASES = frozenset({"coding", "debugging"})


def frozen_by_carrier(ticket: "Ticket") -> str | None:
    """The URL of the open carrier PR freezing *ticket*, else ``None``; a carrier ticket is never frozen."""
    extra = ticket.extra or {}
    carrier_url = extra.get("carried_by")
    if extra.get("carries") or not carrier_url:
        return None
    pull_request = cast("type[PullRequest]", apps.get_model("core", "PullRequest"))
    live_states = (
        pull_request.State.OPEN,
        pull_request.State.REVIEW_REQUESTED,
        pull_request.State.APPROVED,
    )
    return carrier_url if pull_request.objects.filter(url=carrier_url, state__in=live_states).exists() else None


def refuse_frozen_mint(ticket: "Ticket", *, phase: str) -> None:
    if normalize_phase(phase) not in FROZEN_PHASES or (carrier_url := frozen_by_carrier(ticket)) is None:
        return
    msg = (
        f"ticket {ticket.pk} is frozen by carrier {carrier_url}: its {normalize_phase(phase)} work lands in the "
        f"carrier's merge round, so mint nothing until the carrier closes or merges"
    )
    raise InvalidTransitionError(msg)
