"""Did a phase's work LAND? — the evidence a lost lease may not overrule (#3982).

Losing a lease is evidence about the LEASE, never about the WORK. A shipping task that
pushed its branch, opened its pull request and advanced its ticket to ``in_review`` had
completed the phase's entire purpose; recording it ``failed`` / ``lease_lost`` because the
heartbeat lapsed feeds the auto-repair sweep a "needs re-doing" signal for work that is
already done, and inflates the environmental-failure rate capacity decisions read from.

The evidence is read from the DB only — no forge round-trip — so it costs one query on a
path that is already recording an outcome. Three sources, most-authoritative first:

* the ticket's own FSM position, judged on the FULL author ladder
    (:func:`~teatree.core.models.task_phase_disposition.phase_output_reached`);
* for ``shipping``, the phase's artifact — an attached pull request that is still live or
    merged. Re-running shipping against one of those opens a SECOND pull request.
* for a REVIEWER-role ticket's review phase, the phase's artifact is a recorded
    :class:`~teatree.core.models.review_verdict.ReviewVerdict` at the head the task
    reviewed (#4100). Reviewing is where the false failures concentrate, and the author
    ladder can say nothing about a reviewer ticket — it is minted at ``not_started`` and
    held there until ``review_posted`` — so without this the guard structurally could not
    reach the phase that needs it most.

Everything else answers ``""``. An off-ladder state (``review_posted`` / ``ignored``) and a
free-form phase hold no author-ladder position, and a conservative "no evidence" leaves the
caller's existing failure path untouched.
"""

from typing import TYPE_CHECKING

from django.db.models import Q

from teatree.core.modelkit.phase_tools import VERDICT_REVIEW_PHASES
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import ReviewVerdict
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


def phase_landing_evidence(task: "Task", *, trust_phase_artifact: bool) -> str:
    """Describe the evidence that *task*'s phase already landed, or ``""`` when there is none.

    *trust_phase_artifact* scopes the artifact signals, which are weaker than the FSM one.
    An attached non-closed pull request proves a branch was pushed and a PR opened by SOME
    means — but that means need not be a successful ``ship()``: the no-orphan pre-push gate
    and the ``PendingPullRequest`` drain both open a PR independently of the FSM transition.
    A verdict at the reviewed head likewise proves SOME reviewer judged that tree, which
    need not be this task. Trusting either for a genuinely DETERMINISTIC failure (a
    push-gate refusal, missing e2e evidence, a refused verdict recording, ...) would
    silently swallow a real, reproducible defect merely because an artifact happens to
    exist. Only a LOST LEASE is evidence about the lease rather than the work, so only that
    caller may pass ``True`` -- a required keyword rather than a default, so no call site
    can opt in by omission.
    """
    ticket = task.ticket
    if ticket.role == Ticket.Role.REVIEWER:
        return _review_verdict_evidence(task) if trust_phase_artifact else ""
    if ticket.role != Ticket.Role.AUTHOR:
        return ""
    if phase_output_reached(ticket, task.phase):
        # str() before !r: a TextChoices member reprs as ``Ticket.State.IN_REVIEW``,
        # which is not the token the operator reads everywhere else.
        return f"ticket state {str(ticket.state)!r} is at or past the state {task.phase!r} produces"
    if not trust_phase_artifact:
        return ""
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


def _review_verdict_evidence(task: "Task") -> str:
    """The recorded verdict proving *task*'s review landed, or ``""`` — review phases only.

    A verdict binds to the exact tree it judged, so it is evidence only at the head THIS
    task was dispatched against: the #68 auto-review contract carries that head on the
    linked :class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch` row, and
    the external-review path stores it on the ticket as ``reviewed_sha``. A head that
    cannot be resolved leaves the verdict unattributable, which is no evidence at all.
    """
    if normalize_phase(task.phase) not in VERDICT_REVIEW_PHASES:
        return ""
    dispatch = task.auto_review_dispatches.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    reviewed_sha = dispatch.head_sha if dispatch is not None else str((task.ticket.extra or {}).get("reviewed_sha", ""))
    head = reviewed_sha.strip().lower()
    if not head:
        return ""
    bound_to_the_pr = Q(slug=dispatch.slug, pr_id=dispatch.pr_id) if dispatch is not None else Q(ticket=task.ticket)
    verdict = ReviewVerdict.objects.filter(bound_to_the_pr, reviewed_sha=head).first()
    return f"the review verdict {str(verdict.verdict)!r} is recorded at the reviewed head {head[:8]}" if verdict else ""
