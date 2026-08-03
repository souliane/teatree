"""Did a phase's work LAND? — the evidence a lost lease may not overrule (#3982).

Losing a lease is evidence about the LEASE, never about the WORK. A shipping task that
pushed its branch, opened its pull request and advanced its ticket to ``in_review`` had
completed the phase's entire purpose; recording it ``failed`` / ``lease_lost`` because the
heartbeat lapsed feeds the auto-repair sweep a "needs re-doing" signal for work that is
already done, and inflates the environmental-failure rate capacity decisions read from.

The evidence is read from the DB only — no forge round-trip — so it costs one query on a
path that is already recording an outcome. Two sources, most-authoritative first:

* the ticket's own FSM position, judged on the FULL author ladder
    (:func:`~teatree.core.models.task_phase_disposition.phase_output_reached`);
* for ``shipping``, the phase's artifact — an attached pull request that is still live or
    merged. Re-running shipping against one of those opens a SECOND pull request.

Everything else answers ``""``. A reviewer-role ticket, an off-ladder state
(``review_posted`` / ``ignored``) and a free-form phase all hold no author-ladder position,
and a conservative "no evidence" leaves the caller's existing failure path untouched.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task_phase_disposition import phase_output_reached
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.models.task import Task

#: Pull-request states whose row proves the shipping phase produced its artifact. CLOSED is
#: excluded: an abandoned pull request is the one case where shipping genuinely must re-run.
_LANDED_PR_STATES = frozenset(
    {
        PullRequest.State.OPEN,
        PullRequest.State.REVIEW_REQUESTED,
        PullRequest.State.APPROVED,
        PullRequest.State.MERGED,
    },
)


def phase_landing_evidence(task: "Task") -> str:
    """Describe the evidence that *task*'s phase already landed, or ``""`` when there is none."""
    ticket = task.ticket
    if ticket.role != Ticket.Role.AUTHOR:
        return ""
    if phase_output_reached(ticket, task.phase):
        # str() before !r: a TextChoices member reprs as ``Ticket.State.IN_REVIEW``,
        # which is not the token the operator reads everywhere else.
        return f"ticket state {str(ticket.state)!r} is at or past the state {task.phase!r} produces"
    return _shipping_artifact_evidence(task)


def _shipping_artifact_evidence(task: "Task") -> str:
    """The open/merged pull request proving shipping landed, or ``""`` — shipping only.

    The FSM check above misses the case where the phase's work landed but its transition
    did not: the branch is pushed and the pull request is open while the ticket still reads
    ``reviewed``. That is precisely the state a re-dispatch would duplicate.
    """
    if normalize_phase(task.phase) != "shipping":
        return ""
    url = (
        PullRequest.objects.filter(ticket_id=task.ticket_id, state__in=_LANDED_PR_STATES)  # ty: ignore[unresolved-attribute]
        .values_list("url", flat=True)
        .first()
    )
    return f"the shipping artifact exists: pull request {url}" if url else ""
