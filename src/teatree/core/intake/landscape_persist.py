"""The intake landscape survey persisted as a durable artifact before the planner runs."""

import logging

from teatree.config import worktree_root
from teatree.core.intake.landscape_gather import run_landscape
from teatree.core.models import LandscapeArtifact, Ticket

logger = logging.getLogger(__name__)


def persist_intake_landscape(ticket: Ticket) -> None:
    """Bake the intake landscape survey into a durable artifact (#2541).

    Run after the worktrees materialise and before the planner is scheduled, so
    the planner consumes the survey the intake FSM step produced instead of
    re-deriving it. Best-effort context, never a gate: any gather failure (a
    forge outage, a corrupt clone) or an empty survey degrades to a log line —
    it must NEVER abort provisioning or block the planner (fail-open, mirroring
    the landscape module's own degradation doctrine). A survey with only
    warnings is still a non-empty dict, so it is persisted; a gather that raises
    leaves no artifact.
    """
    try:
        survey = run_landscape(worktree_root())
    except Exception:
        logger.warning("Intake landscape gather failed for ticket %s; skipping artifact", ticket.pk, exc_info=True)
        return
    try:
        LandscapeArtifact.record(ticket=ticket, survey=survey, recorded_by="t3:intake")
    except ValueError:
        logger.info("Intake landscape survey for ticket %s was empty; no artifact recorded", ticket.pk)
