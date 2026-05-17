import logging

from teatree.core.models import Ticket
from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class RetroExecutor(RunnerBase):
    """Write retrospection artifacts for a merged ticket.

    Scaffold implementation: records that retro ran and leaves a short marker
    on ``ticket.extra``. The agent-driven retro (skill bundle, prompt build,
    artifact generation) lands in a follow-up PR.
    """

    def __init__(self, ticket: Ticket) -> None:
        self.ticket = ticket

    def run(self) -> RunnerResult:
        ticket = self.ticket
        # #800 N3: canonical locked RMW (was an unlocked extra save).
        ticket.merge_extra(set_keys={"retro_scheduled": True})
        logger.info("Retro scheduled for ticket %s", ticket.pk)
        return RunnerResult(ok=True, detail="retro-scheduled")
