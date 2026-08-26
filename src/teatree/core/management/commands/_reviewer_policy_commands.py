"""``review apply-reviewer-policy`` — the owner surface of the standing reviewer policy.

A mixin on the ``review`` :class:`~django_typer.management.TyperCommand` (django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO), delegating
to :func:`teatree.core.review.reviewer_policy.apply_reviewer_policy` so the policy
lives in the review domain and the command stays a surface.

Reviewer-at-creation cannot reach an MR that is already open, and this is the pass
that catches those up. It takes no username and no author — the reviewers come from
``pr_auto_reviewers`` and the scope from the factory's own identity on the repo — so
it can only ever do the one thing the overlay already configured.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.backends.loader import get_code_host_for_repo
from teatree.core.backend_protocols import BackendResolutionError
from teatree.core.machine_output import emit
from teatree.core.overlay_loader import get_overlay
from teatree.core.review.reviewer_policy import (
    ReviewerAssignable,
    ReviewerPolicyError,
    ReviewerPolicyRow,
    apply_reviewer_policy,
)
from teatree.project import find_project_root
from teatree.utils import git


def _host_for(overlay: object, repo_path: str) -> ReviewerAssignable:
    """The repo's own code host, refusing anything that cannot carry the policy.

    ``get_code_host_for_repo`` is what picks the repo-scoped credential, so the
    identity this resolves as is the same one that authored the MRs being caught up.
    """
    try:
        host = get_code_host_for_repo(overlay, repo_path)  # type: ignore[arg-type]
    except BackendResolutionError as error:
        raise ReviewerPolicyError(str(error)) from error
    if not isinstance(host, ReviewerAssignable):
        msg = f"{repo_path} resolves to a code host that cannot assign reviewers"
        raise ReviewerPolicyError(msg)
    return host


class ReviewerPolicyCommands(TyperCommand):
    @command()
    def apply_reviewer_policy(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Report what would change without writing.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the report rows as JSON.")] = False,
    ) -> list[ReviewerPolicyRow]:
        """Put the overlay's configured reviewers on this repo's open bot-authored MRs.

        Idempotent — an MR already carrying them is left alone. A human-authored MR
        is refused and reported, never assigned. Exits non-zero when an assignment
        it undertook did not land.
        """
        repo_path = str(find_project_root() or ".")
        overlay = get_overlay()
        try:
            host = _host_for(overlay, repo_path)
            report = apply_reviewer_policy(
                overlay,
                host,
                remote=git.remote_url(repo=repo_path),
                dry_run=dry_run,
            )
        except ReviewerPolicyError as error:
            self.stderr.write(f"  refused: {error}")
            raise SystemExit(1) from error

        self.print_result = False
        emit(
            report.rows,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="".join(f"{line}\n" for line in report.lines()),
        )
        if report.failed:
            raise SystemExit(1)
        return report.rows
