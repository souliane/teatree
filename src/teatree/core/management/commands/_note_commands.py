"""``t3 <overlay> ticket comment`` — record a note where its purpose belongs (#162).

A lane reads an issue's DESCRIPTION and never its comments, so a requirement
posted as a comment is silently never executed. This command therefore takes an
explicit ``--purpose`` and routes through
:func:`teatree.core.issue_hygiene.record_issue_note`, which decides body-vs-comment
and refuses a ticket the owner or the factory bot did not file.

It lives here as a :class:`NoteCommands` mixin the ``ticket``
:class:`~django_typer.management.TyperCommand` inherits, so the verb mounts
unchanged while its LOC stays out of the (cap-bound) ``ticket.py`` god-module —
the same split as the sibling ``SweepCommands``.
"""

from typing import Annotated, NoReturn, TypedDict

import typer
from django_typer.management import TyperCommand, command


class CommentResult(TypedDict, total=False):
    issue_url: str
    comment_id: int
    outcome: str
    purpose: str


class NoteCommands(TyperCommand):
    @command()
    def comment(
        self,
        issue_url: str,
        *,
        purpose: Annotated[
            str,
            typer.Option(help="Why: requirement, change_request, scope_change, decision, status, or evidence."),
        ] = "",
        body: Annotated[str, typer.Option(help="Note body text.")] = "",
        body_file: Annotated[str, typer.Option(help="Path to a file containing the note body.")] = "",
        sweep_run_id: Annotated[str, typer.Option(help="Active `ticket sweep-begin` run; refuses every comment.")] = "",
    ) -> CommentResult:
        """Record a note on an issue, in the place its ``--purpose`` belongs (#162).

        A requirement, change request, scope change or decision IS the
        specification, so it appends a dated section to the description where a
        lane reads it — never a comment, which a lane never sees. Only
        ``status`` and ``evidence`` stay comments, and a sweep refuses even
        those. ``--purpose`` has no default: a caller that has not decided
        whether it is writing the spec or reporting on it has not decided where
        the text belongs.

        Resolves the code host per-URL across all registered overlays, so it
        works for any tracker an overlay is configured for. Only tickets the
        owner or the factory bot filed may be changed.
        """
        self.print_result = False
        from pathlib import Path  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415 — deferred: lazy command import
        from teatree.core.issue_hygiene import (  # noqa: PLC0415 — deferred: lazy command import
            IssueWriteConflictError,
            SweepCommentRefusedError,
            record_issue_note,
        )
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.core.self_forge_identities import (  # noqa: PLC0415 — deferred: lazy command import
            ExternalIssueRefusedError,
        )

        text = Path(body_file).read_text(encoding="utf-8") if body_file else body
        if not text:
            self._refuse_note("No note body: pass --body or --body-file")
        host = next(
            (
                found
                for overlay in get_all_overlays().values()
                if (found := get_code_host_for_url(overlay, issue_url)) is not None
            ),
            None,
        )
        if host is None:
            self._refuse_note(f"No code host could be resolved for {issue_url}")
        try:
            outcome = record_issue_note(
                host=host,
                issue_url=issue_url,
                purpose=purpose,
                content=text,
                sweep_run_id=sweep_run_id,
            )
        except (
            ValueError,
            ExternalIssueRefusedError,
            IssueWriteConflictError,
            SweepCommentRefusedError,
        ) as exc:
            self._refuse_note(str(exc))
        verb = "appended to the description of" if outcome.kind == "appended" else "commented on"
        self.stdout.write(f"  {verb} {issue_url}")
        return {
            "issue_url": issue_url,
            "outcome": outcome.kind,
            "purpose": outcome.purpose.value,
            "comment_id": outcome.comment_id,
        }

    def _refuse_note(self, reason: str) -> NoReturn:
        self.stderr.write(f"  refused: {reason}")
        raise SystemExit(1)
