"""``t3 <overlay> ticket rework-hold`` — re-queue the findings of a self-review HOLD a ticket was parked past.

A :class:`ReworkHoldCommands` mixin the ``ticket`` command inherits (the same MRO split as
``TargetBranchCommands``), so its LOC stays out of the cap-bound ``ticket.py``.
"""

from pathlib import Path
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.forge_pr_probe import find_open_pr_for_branch
from teatree.core.machine_output import emit
from teatree.core.models import Ticket
from teatree.core.models.errors import SelfReviewReworkRefusedError
from teatree.core.models.self_review import OPEN_PR_HINT, SelfReview


class SupersededTask(TypedDict):
    task: int
    phase: str
    status: str


class ReworkHoldResult(TypedDict, total=False):
    ticket_id: int
    state: str
    held_task: int
    reviewed_sha: str
    findings: int
    rework_task: int | None
    supersedes: list[SupersededTask]
    dry_run: bool
    error: str
    hint: str


class ReworkHoldCommands(TyperCommand):
    """The ``ticket rework-hold`` command, mounted via MRO inheritance."""

    @command(name="rework-hold")
    def rework_hold(
        self,
        ticket_id: int,
        *,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Report the rework without writing it.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the outcome as JSON.")] = False,
    ) -> ReworkHoldResult:
        """Supersede the ticket's active tasks and queue one coding task carrying its held self-review's findings."""
        result = _requeue(ticket_id, dry_run=dry_run)
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_human(result),
        )
        return result


def _requeue(ticket_id: int, *, dry_run: bool) -> ReworkHoldResult:
    try:
        ticket = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {"error": f"Ticket {ticket_id} not found", "hint": "Pass a ticket id from `ticket list`."}
    try:
        review = SelfReview.held_for_rework(ticket)
        _refuse_a_branch_backing_an_open_pr(ticket)
        rework, superseded = ticket.requeue_self_review_rework(review, dry_run=dry_run)
    except SelfReviewReworkRefusedError as exc:
        return {"error": str(exc), "hint": exc.hint}
    return ReworkHoldResult(
        ticket_id=int(ticket.pk),
        state=ticket.state,
        held_task=review.task_pk,
        reviewed_sha=review.reviewed_sha,
        findings=len(review.findings),
        rework_task=rework.pk if rework is not None else None,
        supersedes=[SupersededTask(task=task.pk, phase=task.phase, status=task.status) for task in superseded],
        dry_run=dry_run,
    )


def _refuse_a_branch_backing_an_open_pr(ticket: Ticket) -> None:
    """The forge half of the open-PR refusal: a PR the push hook opened has no row until something records it."""
    for worktree in ticket.worktrees.all():
        checkout = worktree.worktree_path
        url = (
            find_open_pr_for_branch(checkout, worktree.branch).url_or_none_on_unknown()
            if checkout and Path(checkout).is_dir()
            else None
        )
        if url is None:
            msg = f"could not ask the forge whether branch {worktree.branch!r} backs an open pull request"
            raise SelfReviewReworkRefusedError(msg, hint=_FORGE_UNKNOWN_HINT)
        if url:
            msg = f"ticket {ticket.pk} has an open pull request ({url})"
            raise SelfReviewReworkRefusedError(msg, hint=OPEN_PR_HINT)


_FORGE_UNKNOWN_HINT = "Retry once the forge answers and the branch's checkout is on disk."


def _human(result: ReworkHoldResult) -> str:
    if "error" in result:
        return f"  rework-hold refused: {result['error']}\n  {result['hint']}\n"
    rework = result["rework_task"]
    queued = "would queue a rework task" if rework is None else f"rework task {rework}"
    superseding = "".join(
        f"\n  superseding task {row['task']} ({row['phase']}, {row['status']})" for row in result["supersedes"]
    )
    return (
        f"  ticket {result['ticket_id']}: {queued} carrying {result['findings']} finding(s) of the HOLD at "
        f"{result['reviewed_sha']} (reviewing task {result['held_task']}){superseding}\n"
    )
