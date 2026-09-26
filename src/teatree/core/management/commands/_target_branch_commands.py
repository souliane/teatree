"""``t3 <overlay> ticket set-target-branch`` — the stacked-delivery override setter.

A :class:`TargetBranchCommands` mixin the ``ticket`` command inherits (the same
MRO split as ``CloseCommands``), so its LOC stays out of the cap-bound
``ticket.py``: django-typer collects ``@command`` methods from every
``TyperCommand`` base in the MRO, mounting ``t3 <overlay> ticket
set-target-branch`` with the CLI surface unchanged.

Writes ``Ticket.extra['target_branch'][repo_slug]`` — the per-repo override
:func:`teatree.core.worktree.target_branch.resolve_pr_target_branch` and the
merge-prediction gates already consume — through the locked ``merge_extra``
seam. A stack layer sets it to its parent layer's branch so the shipped PR
targets the stack tip instead of the repo default (``/t3:ship`` § "Stacked
Delivery — One Stack Per Repo (Default)").
"""

from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.merge import normalize_repo_slug
from teatree.core.models import Ticket
from teatree.core.worktree.target_branch import bare_target


class TargetBranchResult(TypedDict):
    ticket_id: int
    repo_slug: str
    target_branch: str


class TargetBranchCommands(TyperCommand):
    """The ``ticket set-target-branch`` command, mounted via MRO inheritance."""

    @command(name="set-target-branch")
    def set_target_branch(
        self,
        ticket_id: int,
        repo: str,
        branch: str,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the outcome as JSON.")] = False,
    ) -> TargetBranchResult:
        """Point *repo*'s future PR at *branch* (its stack parent) instead of the repo default."""
        repo_slug = normalize_repo_slug(repo)
        if not repo_slug:
            self.stderr.write("  set-target-branch refused: repo must be an owner/repo slug or forge URL.")
            raise SystemExit(1)
        cleaned = bare_target(branch)
        if not cleaned:
            self.stderr.write("  set-target-branch refused: a non-empty branch is required.")
            raise SystemExit(1)
        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None
        ticket.merge_extra(merge_into_dicts={"target_branch": {repo_slug: cleaned}})
        result = TargetBranchResult(ticket_id=int(ticket.pk), repo_slug=repo_slug, target_branch=cleaned)
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"  ticket {ticket.pk} PR for {repo_slug} now targets '{cleaned}'",
        )
        return result
