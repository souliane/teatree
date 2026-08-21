"""``t3 <overlay> ticket bulk-close`` / ``fold`` / ``fold-check`` / ``integration-review-override``.

Factored out of ``ticket.py`` as a :class:`CloseCommands` mixin (the module-health
LOC cap), exactly like ``RubricCommands`` / ``ContextCommands``: django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO, so
these mount under ``t3 <overlay> ticket bulk-close`` / ``fold`` / ``fold-check`` /
``integration-review-override`` with the CLI surface unchanged.

``bulk-close`` closes (``ignore``) a batch of tickets behind the no-bulk-close
guard (:func:`teatree.core.gates.bulk_close_gate.check_bulk_close`); ``fold`` and
``fold-check`` are the close's precondition under the backlog sweep's group-first
posture (#4344) — a member's body moves into its host verbatim, and the host is
re-read and proved to carry it before the standalone row is retired;
``integration-review-override`` records the audited escape hatch for the
cross-repo integration-review gate.
"""

from pathlib import Path
from typing import Annotated, TypedDict

import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command

from teatree.core.gates.bulk_close_gate import check_bulk_close
from teatree.core.gates.fold_preservation import check_fold_preserved, fold_body
from teatree.core.models import Ticket


class BulkCloseResult(TypedDict, total=False):
    closed: bool
    closed_ids: list[int]
    refused: bool
    reason: str


class FoldResult(TypedDict, total=False):
    folded: bool
    member_ref: str
    out: str


class FoldCheckResult(TypedDict, total=False):
    preserved: bool
    reason: str


class IntegrationReviewOverrideResult(TypedDict, total=False):
    ticket_id: int
    reason: str


class CloseCommands(TyperCommand):
    """Mixin holding the PR-08 close-flow commands."""

    @command(name="bulk-close")
    def bulk_close(
        self,
        *,
        ids: Annotated[str, typer.Option("--ids", help="Comma-separated ticket ids to close (ignore).")] = "",
        confirm: Annotated[
            str,
            typer.Option("--confirm", help="Comma-separated per-item confirmation tokens (each an id)."),
        ] = "",
    ) -> BulkCloseResult:
        """Close (``ignore``) a batch of tickets, gated by the no-bulk-close guard (PR-08).

        A batch of more than ``bulk_close_threshold`` tickets is refused unless
        every id is echoed in ``--confirm`` — so a mis-scoped sweep cannot
        mass-close silently. A batch at or under the threshold needs no
        confirmation.
        """
        target_ids = [chunk.strip() for chunk in ids.split(",") if chunk.strip()]
        confirmed = [chunk.strip() for chunk in confirm.split(",") if chunk.strip()]
        if not target_ids:
            self.stderr.write("  bulk-close refused: --ids is required (comma-separated ticket ids)")
            raise SystemExit(1)

        refusal = check_bulk_close(items=target_ids, confirmed_tokens=confirmed)
        if refusal:
            self.stdout.write(f"  {refusal}")
            return {"refused": True, "reason": refusal}

        closed: list[int] = []
        try:
            with transaction.atomic():
                for raw_id in target_ids:
                    ticket = self._resolve(int(raw_id))
                    ticket.ignore()
                    ticket.save()
                    closed.append(int(ticket.pk))
        except TransitionNotAllowed as exc:
            # The atomic block already rolled back, so nothing was closed — a
            # ticket in the batch (e.g. an already-DELIVERED/IGNORED one) cannot
            # transition to IGNORED. Surface it cleanly instead of a traceback.
            refusal = f"a ticket cannot be closed from its current state ({exc}); no tickets were closed"
            self.stdout.write(f"  bulk-close refused: {refusal}")
            return {"refused": True, "reason": refusal}
        self.stdout.write(f"  closed {len(closed)} ticket(s): {', '.join(str(cid) for cid in closed)}")
        return {"closed": True, "closed_ids": closed}

    @command(name="fold")
    def fold(
        self,
        *,
        host_body: Annotated[str, typer.Option("--host-body", help="Path to the host ticket's current body.")] = "",
        member_body: Annotated[str, typer.Option("--member-body", help="Path to the folded member's body.")] = "",
        member_ref: Annotated[str, typer.Option("--member-ref", help="The member's ref, e.g. `#4247`.")] = "",
        member_title: Annotated[str, typer.Option("--member-title", help="The member's title.")] = "",
        out: Annotated[str, typer.Option("--out", help="Where to write the merged host body.")] = "",
    ) -> FoldResult:
        """Merge a member ticket's body into its host's, verbatim (#4344).

        The sweep groups aggressively and closes nothing for real, so a member row is
        retired only after this has moved its substance into an existing host. The copy
        is verbatim and idempotent per ``--member-ref``, which is what makes the
        ``fold-check`` re-read afterwards a real proof rather than a formality.
        """
        missing = [
            flag
            for flag, value in (
                ("--host-body", host_body),
                ("--member-body", member_body),
                ("--member-ref", member_ref),
            )
            if not value.strip()
        ]
        if missing or not out.strip():
            required = ", ".join([*missing, *([] if out.strip() else ["--out"])])
            self.stderr.write(f"  fold refused: {required} is required")
            raise SystemExit(1)

        merged = fold_body(
            host_body=self._read(host_body),
            member_ref=member_ref.strip(),
            member_title=member_title,
            member_body=self._read(member_body),
        )
        Path(out).write_text(merged, encoding="utf-8")
        # django-typer str()-es a handler's return onto stdout unless this is pinned, which
        # would put a Python repr on the data channel beside the human line below.
        self.print_result = False
        self.stdout.write(f"  folded {member_ref.strip()} into the host body -> {out}")
        return {"folded": True, "member_ref": member_ref.strip(), "out": out}

    @command(name="fold-check")
    def fold_check(
        self,
        *,
        host_body: Annotated[str, typer.Option("--host-body", help="Path to the host body to prove.")] = "",
        member_body: Annotated[str, typer.Option("--member-body", help="Path to the folded member's body.")] = "",
    ) -> FoldCheckResult:
        """Prove a host body still carries the folded member's substance (#4344).

        Run it against the host body re-read off the forge, before retiring the member's
        row: a host that summarised instead of moving the body fails here, so the close
        that would have discarded the idea never happens. Exits non-zero on a loss.
        """
        if not host_body.strip() or not member_body.strip():
            self.stderr.write("  fold-check refused: --host-body and --member-body are required")
            raise SystemExit(1)

        refusal = check_fold_preserved(member_body=self._read(member_body), host_body=self._read(host_body))
        if refusal:
            self.stdout.write(f"  {refusal}")
            raise SystemExit(1)
        self.print_result = False
        self.stdout.write("  fold preserved: every line of the member's body is in the host body")
        return {"preserved": True}

    @command(name="integration-review-override")
    def integration_review_override(
        self,
        ticket_id: int,
        *,
        reason: Annotated[str, typer.Option("--reason", help="Why this >=2-repo ticket is exempt.")] = "",
    ) -> IntegrationReviewOverrideResult:
        """Record the audited escape hatch for the cross-repo integration-review gate (PR-08).

        Sets ``extra['integration_review_override']`` so ``mark_delivered`` lets a
        legitimately-exempt >=2-repo ticket close without an integration-review
        artifact. A blank reason is refused — the override must be attributable.
        """
        if not reason.strip():
            self.stderr.write("  integration-review-override refused: --reason is required")
            raise SystemExit(1)
        ticket = self._resolve(ticket_id)
        ticket.merge_extra(set_keys={"integration_review_override": {"reason": reason.strip()}})
        self.stdout.write(f"  recorded integration-review override for ticket {ticket_id}")
        return {"ticket_id": ticket_id, "reason": reason.strip()}

    def _read(self, path: str) -> str:
        """Read a body file or abort the subcommand with a nonzero exit."""
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            self.stderr.write(f"  cannot read {path}: {exc}")
            raise SystemExit(1) from None

    def _resolve(self, ticket_id: int) -> Ticket:
        """Fetch a ticket or abort the subcommand with a nonzero exit."""
        try:
            return Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None
