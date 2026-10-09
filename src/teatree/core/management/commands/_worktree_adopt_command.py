"""The ``worktree adopt`` operator command.

A checkout that exists on disk but carries no ``Worktree`` row — cut by ``git
worktree add``, by another session, or left behind when its row was reaped — could
not be operated on through ``t3`` at all: ``run tests`` refused it, and the only
creating verb was ``provision``, which runs tenant-DB provisioning the overlay
forbids for backend-only unit-test work. Registration existed, but only as a SIDE
EFFECT of resolving something else, so a checkout the implicit chain could not
classify had no door at all. This is that door, and it does exactly one thing:
record the row.

A :class:`WorktreeAdoptCommands` mixin the ``worktree``
:class:`~django_typer.management.TyperCommand` inherits from, mirroring
:class:`~teatree.core.management.commands._worktree_occupancy.OccupancyCommands` —
django-typer collects ``@command`` methods from every ``TyperCommand`` base in the
MRO, so the leaf mounts as ``t3 <overlay> worktree adopt`` while its LOC stays out of
the cap-bound ``worktree.py``.
"""

from pathlib import Path
from typing import IO, Annotated, NoReturn, cast

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.core.intake.auto_ticket import synthetic_branch_ticket
from teatree.core.intake.resolve import (
    TicketIdentityCollisionError,
    _get_user_cwd,
    _overlay_name_for_cwd,
    _ticket_by_number,
    _ticket_owning_branch,
    _workspace_owner_ticket,
)
from teatree.core.machine_output import emit
from teatree.core.management.commands._worktree_result_types import AdoptResult
from teatree.core.models import Ticket
from teatree.core.provision.worktree_adopt import (
    WorktreeAdoptError,
    adopt_worktree_for_ticket,
    assert_adoptable,
    path_owner_for,
    repo_name_for_checkout,
)
from teatree.utils import git


class WorktreeAdoptCommands(TyperCommand):
    """The adopt-an-existing-checkout command surface (mixed into ``worktree``)."""

    @command()
    def adopt(
        self,
        path: Annotated[
            str,
            typer.Argument(help="The checkout to register (defaults to the invoking directory)."),
        ] = "",
        ticket: Annotated[
            str,
            typer.Option(help="Attach the row to this ticket number instead of auto-attributing it."),
        ] = "",
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the adopted row as JSON on stdout instead of the human view."),
        ] = False,
    ) -> AdoptResult:
        """Register an existing on-disk checkout as a ``Worktree`` row.

        Creates the row and nothing else — no provisioning, no DSLR, no database
        work — so a worktree cut outside ``t3`` becomes testable through ``t3``
        without running what the overlay forbids for unit-test work.

        Repo and branch come from the checkout itself. Refused when the path is not
        a linked git worktree this venue can query, when it sits on a default
        branch, or when any row already records it — two rows claiming one directory
        is the collision core forecloses everywhere.
        """
        target = Path(path).expanduser().resolve() if path else Path(_get_user_cwd()).resolve()
        self._refuse_if_claimed(target)
        self._refuse_unless_adoptable(target)
        branch = git.current_branch(repo=str(target))
        # A refusal after an auto: ticket was resolved must roll that ticket back with it.
        with transaction.atomic():
            owner = self._owning_ticket(target, branch=branch, named=ticket)
            try:
                worktree = adopt_worktree_for_ticket(owner, cwd=str(target))
            except WorktreeAdoptError as exc:
                self._refuse(str(exc))
        result = AdoptResult(
            worktree_id=int(worktree.pk),
            ticket_id=int(owner.pk),
            repo_path=worktree.repo_path,
            branch=worktree.branch,
            path=str(target),
        )
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=(
                f"  adopted {target} as worktree {worktree.pk} (repo {worktree.repo_path}, "
                f"branch {worktree.branch}, ticket {owner.pk}) — not provisioned\n"
            ),
        )
        return result

    def _refuse(self, message: str) -> NoReturn:
        self.stderr.write(f"  Refused: {message}")
        raise SystemExit(1)

    def _refuse_if_claimed(self, target: Path) -> None:
        """Refuse before any ticket is resolved, so a refusal forks no ``auto:`` ticket."""
        claimed = path_owner_for(target)
        if claimed is not None:
            self._refuse(
                f"worktree {claimed.pk} (ticket {claimed.ticket.pk}) already records {target}. "
                "Inspect it with `t3 <overlay> worktree status`."
            )

    def _refuse_unless_adoptable(self, target: Path) -> None:
        try:
            assert_adoptable(target)
        except WorktreeAdoptError as exc:
            self._refuse(str(exc))

    def _owning_ticket(self, target: Path, *, branch: str, named: str) -> Ticket:
        """The ticket this checkout adopts onto — named, else attributed, else synthetic.

        The attribution order mirrors the implicit registration chain (branch-encoded
        number, then the workspace-dir owner) so adopting a checkout by hand lands it
        on the same ticket auto-registration would have chosen; only the ``auto:``
        fork differs in that it is reached deliberately here.
        """
        overlay_name = _overlay_name_for_cwd(target)
        try:
            if named:
                hint = _ticket_by_number(named, overlay=overlay_name)
                if hint is None:
                    self._refuse(f"no ticket found for --ticket {named}.")
                return hint
            attributed = _ticket_owning_branch(branch, overlay=overlay_name) or _workspace_owner_ticket(target)
        except TicketIdentityCollisionError as exc:
            self._refuse(f"{exc} Name the ticket explicitly with --ticket.")
        if attributed is not None:
            return attributed
        # The CLONE's leaf, never the directory's: a hand-made `git worktree add`
        # yields a branch-named directory, and that name is the key every repo lookup matches on.
        repo_name = repo_name_for_checkout(target)
        return synthetic_branch_ticket(branch, repo_name=repo_name, overlay_name=overlay_name or "")
