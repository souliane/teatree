"""The live forge state of the PR a reviewer ticket reviews, registered for the model layer.

``teatree.core.models`` cannot import the backend registry or the overlay loader, so
``populate_model_registries`` registers :func:`read_pr_open_state` as a resolver and the
review mint fetches it by name.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.backend_protocols import PrOpenState
from teatree.core.backend_registry import get_backend_provider
from teatree.core.overlay_loader import get_overlay_for_ticket

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


def read_pr_open_state(ticket: "Ticket") -> PrOpenState:
    """One bounded forge read; every failure is ``UNKNOWN``, which the caller treats as not-open."""
    try:
        host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(ticket), ticket.issue_url)
        if host is None:
            return PrOpenState.UNKNOWN
        return host.get_pr_open_state(pr_url=ticket.issue_url)
    except Exception:
        logger.warning("Could not read PR state for %s", ticket.issue_url, exc_info=True)
        return PrOpenState.UNKNOWN
