import logging

from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class RetroExecutor(RunnerBase):
    """Write retrospection artifacts for a merged ticket.

    Scaffold implementation: records that retro ran and leaves a short marker
    on ``ticket.extra``. The agent-driven retro (skill bundle, prompt build,
    artifact generation) lands in a follow-up PR.
    """

    def run(self) -> RunnerResult:
        ticket = self.ticket
        extra = dict(ticket.extra or {})
        extra["retro_scheduled"] = True
        ticket.extra = extra
        ticket.save(update_fields=["extra"])
        logger.info("Retro scheduled for ticket %s", ticket.pk)
        return RunnerResult(ok=True, detail="retro-scheduled")
