"""The IMPLEMENT phase — synthetic ticket + normal maker pipeline (T4-PR-3).

Cloned from :func:`teatree.loops.dream.umbrella_ledger.schedule_gap_fix`: an
admitted experiment anchors an ``AUTHOR`` ``Ticket`` on a unique synthetic issue
URL and rides :meth:`Ticket.schedule_coding` — the SAME isolated-worktree → plan
gate → phase agents → review → critic-gated merge keystone every ticket uses. The
critic therefore supervises every experiment merge for free (the ``mark_delivered``
FSM condition); the outer loop gains ZERO new merge authority.
"""

from teatree.core.models import OuterLoopExperiment
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

#: The standing umbrella issue every outer-loop experiment's synthetic ticket
#: anchors under; the ``#outer-loop-experiment=<pk>`` fragment makes each unique
#: while still resolving the ``souliane/teatree`` overlay via ``infer_overlay_for_url``.
OUTER_LOOP_UMBRELLA_URL = "https://github.com/souliane/teatree/issues/3009"

_EXPERIMENT_KEY = "outer_loop_experiment_id"
_TARGET_KEY = "outer_loop_target"


def _experiment_issue_url(experiment: OuterLoopExperiment, *, umbrella_url: str) -> str:
    return f"{umbrella_url}#outer-loop-experiment={experiment.pk}"


def schedule_experiment_fix(
    experiment: OuterLoopExperiment,
    *,
    umbrella_url: str = OUTER_LOOP_UMBRELLA_URL,
) -> Task | None:
    """Anchor the experiment's synthetic ticket + schedule its coding task.

    Idempotent per experiment (the synthetic issue URL dedups); transitions the
    experiment ``ADMITTED`` → ``IMPLEMENTING`` and returns the scheduled ``Task``
    (``None`` when a coding task already exists for the ticket).
    """
    issue_url = _experiment_issue_url(experiment, umbrella_url=umbrella_url)
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"role": Ticket.Role.AUTHOR, "short_description": experiment.hypothesis.strip()[:80]},
    )
    extra = dict(ticket.extra or {})
    extra.update({_EXPERIMENT_KEY: experiment.pk, _TARGET_KEY: experiment.target_provider_id})
    if extra != ticket.extra:
        ticket.extra = extra
        ticket.save(update_fields=["extra"])
    task: Task | None = None
    already_scheduled = Task.objects.pending_in_phase("coding").filter(ticket=ticket).exists()
    if not already_scheduled and ticket.state == Ticket.State.NOT_STARTED:
        task = ticket.schedule_coding()
    experiment.begin_implementation(ticket)
    return task
