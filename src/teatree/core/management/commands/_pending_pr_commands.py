"""``pr discharge-pending`` — the operator escape for an undischargeable PR obligation.

A mixin on the ``pr`` :class:`~django_typer.management.TyperCommand` (django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO), kept out
of ``pr.py`` so that module stays within the module-health LOC bar.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.management.commands._ensure_pr import DischargeResult


class PendingPrCommands(TyperCommand):
    @command(name="discharge-pending")
    def discharge_pending(
        self,
        obligation_id: int,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the outcome as JSON.")] = False,
    ) -> DischargeResult:
        """Drop a deferred-PR obligation the drain can never discharge.

        The never-lockout escape for ``t3 doctor check``'s pending-PR FAIL: a
        branch abandoned on purpose, or one whose worktree was reaped, owes a PR
        no re-run can open, and without this the doctor stays red on a
        remediation that cannot succeed. Discharging is bookkeeping only — it
        deletes the obligation, never a branch, a PR, or a ticket.
        """
        from teatree.core.models import PendingPullRequest  # noqa: PLC0415 — deferred: ORM/app-registry

        row = PendingPullRequest.objects.filter(pk=obligation_id).first()
        if row is None:
            result = DischargeResult(
                discharged=False,
                error=f"no pending pull request obligation with id {obligation_id}",
            )
            human = f"no pending pull request obligation with id {obligation_id}\n"
        else:
            branch, repo_path = row.branch, row.repo_path
            row.delete()
            result = DischargeResult(discharged=True, branch=branch, repo_path=repo_path)
            human = f"discharged the pull-request obligation for {branch} in {repo_path}\n"
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
        return result


__all__ = ["PendingPrCommands"]
