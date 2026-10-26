"""The IMPLEMENT phase — synthetic ticket + normal maker pipeline (T4-PR-3).

An admitted experiment anchors an ``AUTHOR`` ``Ticket`` on a unique synthetic issue
URL and rides :meth:`Ticket.schedule_implementing` — planning first, then the SAME
isolated-worktree → plan gate → phase agents → review → critic-gated merge keystone
every ticket uses. The
critic therefore supervises every experiment merge for free (the ``mark_delivered``
FSM condition); the outer loop gains ZERO new merge authority.
"""

import logging

from teatree.core.admission.pr_start_cap import open_pr_start_cap_hold_reason
from teatree.core.models import OuterLoopExperiment
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.utils.url_slug import SYNTHETIC_LOOP_UMBRELLA_URL

#: The standing umbrella issue every outer-loop experiment's synthetic ticket
#: anchors under; the ``#outer-loop-experiment=<pk>`` fragment makes each unique
#: while retaining its work's overlay independently of the umbrella URL.
#: Single-sourced from the ONE umbrella anchor the task sweep recognises (#3706).
OUTER_LOOP_UMBRELLA_URL = SYNTHETIC_LOOP_UMBRELLA_URL

_EXPERIMENT_KEY = "outer_loop_experiment_id"
_TARGET_KEY = "outer_loop_target"
logger = logging.getLogger(__name__)


def _experiment_issue_url(experiment: OuterLoopExperiment, *, umbrella_url: str) -> str:
    return f"{umbrella_url}#outer-loop-experiment={experiment.pk}"


def schedule_experiment_fix(
    experiment: OuterLoopExperiment,
    *,
    umbrella_url: str = OUTER_LOOP_UMBRELLA_URL,
) -> Task | None:
    """Anchor the experiment's synthetic ticket + schedule its implementation, planned first.

    Idempotent per experiment (the synthetic issue URL dedups); transitions the
    experiment ``ADMITTED`` → ``IMPLEMENTING`` and returns the scheduled ``Task``
    (``None`` once the ticket has left NOT_STARTED or already carries a coding task).
    """
    issue_url = _experiment_issue_url(experiment, umbrella_url=umbrella_url)
    if reason := open_pr_start_cap_hold_reason():
        logger.warning("outer loop is claiming nothing new: %s", reason)
        return None
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={
            "role": Ticket.Role.AUTHOR,
            "short_description": experiment.hypothesis.strip()[:80],
            "overlay": Ticket.creation_overlay(experiment.overlay),
        },
    )
    ticket.merge_extra(set_keys={_EXPERIMENT_KEY: experiment.pk, _TARGET_KEY: experiment.target_provider_id})
    task: Task | None = None
    already_scheduled = Task.objects.pending_in_phase("coding").filter(ticket=ticket).exists()
    if not already_scheduled and ticket.state == Ticket.State.NOT_STARTED:
        task = ticket.schedule_implementing(
            "coding",
            reason=f"Outer-loop experiment {experiment.pk} — {experiment.hypothesis.strip()}",
        )
    experiment.begin_implementation(ticket)
    return task
