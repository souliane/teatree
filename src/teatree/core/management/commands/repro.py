"""``t3 <overlay> repro`` — record executed RED->GREEN reproduction evidence (#118).

The harness-recorded seam behind the forced-repro gate. The agent supplies only
the repro *command string*; the harness supplies the exit code and the SHAs — it
stamps ``git rev-parse HEAD`` and runs the command itself, so a fabricated exit
code or SHA cannot enter the record. There is deliberately NO ``--exit-code`` /
``--head-sha`` override on record-red / record-green: letting the agent supply
those would defeat the whole anti-fabrication point.

*   ``record-red``   — stamp HEAD, run the command in the ticket's worktree,
    record the FAILING result (the factory refuses exit 0).
*   ``record-green`` — stamp the new HEAD, run the same command, compute
    ``merge-base --is-ancestor red green``, record the PASSING result with the
    frozen provenance verdict (the factory refuses a still-failing command, a
    ``red == green`` capture, or a non-ancestor RED tree).
*   ``waive``        — record a HUMAN-authorized ``ReproWaiver`` (maker != checker).
*   ``status``       — read-only audit of the RED/GREEN/provenance state.
"""

from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import Ticket
from teatree.core.models.repro_evidence import HarnessRun, ReproEvidence, ReproEvidenceError
from teatree.core.models.repro_waiver import ReproWaiver, ReproWaiverError
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.utils import git
from teatree.utils.run import run_allowed_to_fail

__all__ = ["Command"]


class ReproRecordResult(TypedDict, total=False):
    """Result of ``repro record-red`` / ``record-green``."""

    recorded: bool
    error: str
    ticket_id: int
    head_sha: str
    exit_code: int
    provenance_ok: bool


class ReproWaiveResult(TypedDict, total=False):
    """Result of ``repro waive``."""

    waived: bool
    error: str
    ticket_id: int
    approver_id: str


def _resolve_cwd(ticket: Ticket, cwd: str) -> str:
    explicit = cwd.strip()
    if explicit:
        return explicit
    return dispatch_worktree_path(ticket) or "."


def _run_repro(command: str, cwd: str, head_sha: str) -> HarnessRun:
    """Run *command* in *cwd* and capture it as a :class:`HarnessRun`.

    The command string is executed through ``bash -c`` so an arbitrary repro
    invocation (a pytest node id, a shell pipeline) runs as written; the
    sanctioned ``run_allowed_to_fail`` wrapper captures the exit code and output,
    which are bundled with the harness-stamped *head_sha* the agent cannot forge.
    """
    result = run_allowed_to_fail(["bash", "-c", command], expected_codes=None, cwd=cwd)
    return HarnessRun(head_sha=head_sha, exit_code=result.returncode, output=result.stdout + result.stderr)


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="record-red")
    def record_red(
        self,
        ticket_id: str,
        *,
        command: Annotated[str, typer.Option("--command", help="The repro command to run (must FAIL pre-fix).")],
        cwd: Annotated[
            str,
            typer.Option("--cwd", help="Worktree dir to run in (default: the ticket's dispatch worktree)."),
        ] = "",
    ) -> "ReproRecordResult":
        """Record the harness-run FAILING RED reproduction for a FIX ticket (#118)."""
        ticket = Ticket.objects.resolve(ticket_id)
        workdir = _resolve_cwd(ticket, cwd)
        run = _run_repro(command, workdir, git.head_sha(repo=workdir))
        try:
            row = ReproEvidence.record_red(ticket=ticket, command=command, run=run)
        except ReproEvidenceError as exc:
            self.stderr.write(f"  repro record-red refused: {exc}")
            return {"recorded": False, "error": str(exc)}
        self.stdout.write(
            f"  recorded RED repro for ticket {ticket.pk} @ {row.red_head_sha[:8]} (exit {row.red_exit_code})"
        )
        return {
            "recorded": True,
            "ticket_id": int(ticket.pk),
            "head_sha": row.red_head_sha,
            "exit_code": row.red_exit_code,
            "provenance_ok": row.provenance_ok,
        }

    @command(name="record-green")
    def record_green(
        self,
        ticket_id: str,
        *,
        command: Annotated[str, typer.Option("--command", help="The repro command to run (must PASS post-fix).")],
        cwd: Annotated[
            str,
            typer.Option("--cwd", help="Worktree dir to run in (default: the ticket's dispatch worktree)."),
        ] = "",
    ) -> "ReproRecordResult":
        """Record the harness-run PASSING GREEN and freeze the provenance verdict (#118).

        Computes ``git merge-base --is-ancestor red green`` in the worktree — the
        proof the RED tree is a proper ancestor of the GREEN tree — and passes it
        to the domain factory, which refuses when it is False (the provenance
        bypass) or when ``red == green``.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        workdir = _resolve_cwd(ticket, cwd)
        green_sha = git.head_sha(repo=workdir)
        run = _run_repro(command, workdir, green_sha)
        red_sha = ReproEvidence.objects.red_sha_for(ticket, command)
        red_is_ancestor = bool(red_sha) and git.check(
            repo=workdir, args=["merge-base", "--is-ancestor", red_sha, green_sha]
        )
        try:
            row = ReproEvidence.record_green(ticket=ticket, command=command, run=run, red_is_ancestor=red_is_ancestor)
        except ReproEvidenceError as exc:
            self.stderr.write(f"  repro record-green refused: {exc}")
            return {"recorded": False, "error": str(exc)}
        self.stdout.write(
            f"  recorded GREEN repro for ticket {ticket.pk} @ {row.green_head_sha[:8]} "
            f"(exit 0, provenance_ok={row.provenance_ok})"
        )
        return {
            "recorded": True,
            "ticket_id": int(ticket.pk),
            "head_sha": row.green_head_sha,
            "exit_code": run.exit_code,
            "provenance_ok": row.provenance_ok,
        }

    @command(name="waive")
    def waive(
        self,
        ticket_id: str,
        *,
        approver: Annotated[str, typer.Option("--approver", help="Human user id (a maker/loop id is refused).")],
        reason: Annotated[str, typer.Option("--reason", help="Why this failure class is genuinely repro-less.")],
    ) -> "ReproWaiveResult":
        """Record a HUMAN-authorized waiver of the forced-repro gate (#118)."""
        ticket = Ticket.objects.resolve(ticket_id)
        try:
            waiver = ReproWaiver.record(ticket=ticket, approver_id=approver, reason=reason)
        except ReproWaiverError as exc:
            self.stderr.write(f"  repro waive refused: {exc}")
            return {"waived": False, "error": str(exc)}
        self.stdout.write(f"  recorded repro waiver for ticket {ticket.pk} by {waiver.approver_id}")
        return {"waived": True, "ticket_id": int(ticket.pk), "approver_id": waiver.approver_id}

    @command(name="status")
    def status(self, ticket_id: str) -> str:
        """Show the recorded RED/GREEN/provenance/waiver state for a ticket (audit)."""
        ticket = Ticket.objects.resolve(ticket_id)
        lines = [f"repro status for ticket {ticket.pk}:"]
        waivers = ReproWaiver.objects.filter(ticket=ticket)
        lines.extend(f"  WAIVER by {waiver.approver_id}: {waiver.reason}" for waiver in waivers)
        rows = ReproEvidence.objects.filter(ticket=ticket)
        if not rows and not waivers:
            lines.append("  (no repro evidence, no waiver)")
        lines.extend(
            f"  RED {row.red_head_sha[:8]} (exit {row.red_exit_code}) -> "
            f"GREEN {row.green_head_sha[:8] if row.green_head_sha else '—'} "
            f"(exit {row.green_exit_code}) provenance_ok={row.provenance_ok}"
            for row in rows
        )
        valid = ReproEvidence.objects.has_valid_repro(ticket)
        lines.append(f"  gate-satisfied: {valid or waivers.exists()}")
        output = "\n".join(lines)
        self.stdout.write(output)
        return output
