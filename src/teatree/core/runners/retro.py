import logging

from teatree.core.gates.owner_merge_evidence import record_forge_merger
from teatree.core.models import Ticket
from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class RetroPhaseMarker(RunnerBase):
    """Stamp the retro phase and read any unaudited forge merger.

    Bookkeeping only — it runs no retrospective. The agent-driven retro is the
    interactive ``/t3:retro`` skill, attested via ``t3 <overlay> lifecycle
    visit-phase <ticket_id> retro`` (the shipping-phase gate block in
    ``teatree.agents.prompt``); no sub-agent phase runs it.
    """

    def __init__(self, ticket: Ticket) -> None:
        self.ticket = ticket

    def run(self) -> RunnerResult:
        ticket = self.ticket
        # #800 N3: canonical locked RMW (was an unlocked extra save).
        ticket.merge_extra(set_keys={"retro_scheduled": True})
        record_forge_merger(ticket)
        logger.info("Retro phase marked for ticket %s", ticket.pk)
        return RunnerResult(ok=True, detail="retro-scheduled")
