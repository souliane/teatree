"""``pr void-merge`` — the human-only correction for a merge whose content never reached the default branch."""

import sys
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.core.merge.void_merge import VoidPlan, VoidRefusedError, apply_void, check_operator, plan_void
from teatree.core.models import MergeVoid, PullRequest, Ticket
from teatree.core.models.merge_void import PullRequestClose, TicketKept, TicketRestore


class VoidMergeResult(TypedDict, total=False):
    pr_url: str
    dry_run: bool
    verdict: str
    changed_paths: int
    absent_paths: int
    merged_sha: str
    content_head: str
    default_tip: str
    tickets: list[TicketRestore]
    kept: list[TicketKept]
    pull_requests: list[PullRequestClose]
    void_id: int
    error: str


def _result(plan: VoidPlan, *, dry_run: bool) -> VoidMergeResult:
    verdict = plan.verdict
    result = VoidMergeResult(
        pr_url=plan.pr_url,
        dry_run=dry_run,
        verdict=str(verdict.kind),
        changed_paths=len(verdict.changed),
        absent_paths=len(verdict.changed) - len(verdict.present_at_tip),
        merged_sha=plan.snapshot.merge_commit,
        content_head=plan.snapshot.content_head,
        default_tip=plan.snapshot.default_tip,
        tickets=list(plan.restores),
        kept=list(plan.kept),
        pull_requests=list(plan.closes),
    )
    if plan.refusal is not None:
        result["error"] = str(plan.refusal)
    return result


def _reread(void: MergeVoid) -> list[str]:
    void.refresh_from_db()
    closed = [close["pull_request_id"] for close in void.evidence["pull_requests"]]
    rows = PullRequest.objects.filter(pk__in=closed).order_by("pk")
    tickets = Ticket.objects.filter(merge_voids=void).order_by("pk")
    return [
        void.describe(),
        *(f"  pull request row {row.pk}: {row.state}" for row in rows),
        *(f"  ticket {ticket.pk}: {ticket.state}" for ticket in tickets),
    ]


class VoidMergeCommands(TyperCommand):
    @command(name="void-merge")
    def void_merge(
        self,
        pr_url: str,
        *,
        reason: Annotated[str, typer.Option(help="Why the recorded merge is void.")] = "",
        by: Annotated[str, typer.Option(help="The human operator voiding it.")] = "",
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Print the forge evidence and planned changes; write nothing.")
        ] = False,
    ) -> VoidMergeResult:
        """Void a merge the forge records but whose content never reached the default branch."""
        self.print_result = False
        try:
            if not dry_run:
                check_operator(reason=reason, actor=by)
            plan = plan_void(pr_url)
            self.stdout.write("\n".join(plan.describe()))
            if dry_run:
                return _result(plan, dry_run=True)
            void = apply_void(plan, reason=reason, actor=by, stdin=sys.stdin, stdout=sys.stdout)
        except VoidRefusedError as exc:
            self.stderr.write(str(exc))
            return VoidMergeResult(pr_url=pr_url, error=str(exc))
        self.stdout.write("\n".join(_reread(void)))
        result = _result(plan, dry_run=False)
        result["void_id"] = void.pk
        return result
