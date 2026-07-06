"""Harness-recorded executed RED->GREEN reproduction evidence for FIX tickets (#118).

Without an *executed* failing reproduction, an agent's root-cause explanation is
wrong roughly half the time; forcing a confirming repro removes most of that
error. teatree encoded "verify against ground truth" only as behavioural memory.
#118 promotes it to a deterministic FSM gate: a FIX ticket cannot ship unless a
harness-recorded RED (failing) -> GREEN (passing) reproduction exists whose RED
was provably captured against a tree that did NOT contain the fix.

The anti-fabrication enforcement lives in the guarded factories here, not in
prose. The harness — not the agent — runs both commands and stamps both SHAs
from ``git rev-parse HEAD``, so exit codes and SHAs cannot be forged:

*   :meth:`record_red` refuses a command that exited 0 (a passing command is not
    a failing repro) and refuses re-recording RED after GREEN (tamper).
*   :meth:`record_green` refuses a still-failing command (the fix did not fix
    it), refuses when no matching RED exists, refuses ``red == green`` (RED
    captured against the same tree as GREEN), and refuses when the RED tree is
    not a proper ancestor of the GREEN tree (the provenance-bypass refusal). The
    ancestry result is frozen into ``provenance_ok`` at record time so the gate
    is a pure DB read.

``merge-base --is-ancestor red green`` succeeding *with* ``red != green`` means
every commit unique to GREEN (the fix) is absent from the RED tree, so the RED
necessarily ran against a tree lacking the fix. That ancestry — not the server
clock — is the load-bearing proof; ``red_recorded_at < green_recorded_at`` is a
secondary human-audit signal only.
"""

import hashlib
from dataclasses import dataclass
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import is_commit_sha
from teatree.core.models.ticket import Ticket

_OUTPUT_TAIL_MAX = 4000


class ReproEvidenceError(ValueError):
    """A ``ReproEvidence`` factory rejected a record — the anti-fabrication contract failed."""


@dataclass(frozen=True, slots=True)
class HarnessRun:
    """The harness-captured result of one repro execution, travelling as a unit.

    The harness — not the agent — stamps *head_sha* (``git rev-parse HEAD``) and
    captures *exit_code* + *output* by running the command itself, so these three
    cannot be forged in prose. Bundling them keeps the guarded factories to a
    single execution argument.
    """

    head_sha: str
    exit_code: int
    output: str


def _canonical_sha(head_sha: str) -> str:
    return head_sha.strip().lower()


def _command_fingerprint(command: str) -> str:
    """sha256 of the whitespace-normalized command — the RED/GREEN pairing key."""
    return hashlib.sha256(" ".join(command.split()).encode()).hexdigest()


def _output_digest(output: str) -> str:
    return hashlib.sha256(output.encode()).hexdigest()


class ReproEvidenceManager(models.Manager["ReproEvidence"]):
    def red_sha_for(self, ticket: Ticket, command: str) -> str:
        """The recorded RED head SHA for ``(ticket, command)``, or '' when none.

        The CLI's ancestry seam: it needs the RED SHA to run ``git merge-base
        --is-ancestor red green`` before calling :meth:`ReproEvidence.record_green`,
        without reaching into the private fingerprint helper.
        """
        row = self.filter(ticket=ticket, command_fingerprint=_command_fingerprint(command)).first()
        return row.red_head_sha if row is not None else ""

    def has_valid_repro(self, ticket: Ticket) -> bool:
        """True iff *ticket* has a provenance-verified RED->GREEN pair.

        The gate's single read: a row with a non-zero RED exit, a zero GREEN
        exit, a recorded GREEN SHA, and ``provenance_ok=True``. A hand-crafted
        row with ``provenance_ok=False`` never satisfies this — the ancestry
        proof is frozen at record time, so the gate cannot be tricked by a
        directly-written row that skipped the factory.
        """
        return (
            self.filter(ticket=ticket, provenance_ok=True, green_exit_code=0)
            .exclude(red_exit_code=0)
            .exclude(green_head_sha="")
            .exists()
        )


class ReproEvidence(models.Model):
    """One executed RED->GREEN reproduction for a ticket's repro command (#118)."""

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="repro_evidences",
    )
    command = models.TextField()
    command_fingerprint = models.CharField(max_length=64, db_index=True)
    red_head_sha = models.CharField(max_length=64)
    red_exit_code = models.IntegerField()
    red_output_digest = models.CharField(max_length=64)
    red_output_tail = models.TextField(blank=True, default="")
    red_recorded_at = models.DateTimeField(default=timezone.now)
    green_head_sha = models.CharField(max_length=64, blank=True, default="")
    green_exit_code = models.IntegerField(null=True, blank=True)
    green_output_digest = models.CharField(max_length=64, blank=True, default="")
    green_output_tail = models.TextField(blank=True, default="")
    green_recorded_at = models.DateTimeField(null=True, blank=True)
    provenance_ok = models.BooleanField(default=False)

    objects: ClassVar[ReproEvidenceManager] = ReproEvidenceManager()

    class Meta:
        db_table = "teatree_repro_evidence"
        ordering: ClassVar = ["-red_recorded_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["ticket", "command_fingerprint"],
                name="uniq_repro_evidence_ticket_cmd",
            )
        ]

    def __str__(self) -> str:
        green = self.green_head_sha[:8] if self.green_head_sha else "—"
        return f"repro<ticket={self.ticket_id} {self.red_head_sha[:8]}->{green} ok={self.provenance_ok}>"  # ty: ignore[unresolved-attribute]

    @classmethod
    def record_red(cls, *, ticket: Ticket, command: str, run: HarnessRun) -> "ReproEvidence":
        """Record the harness-run failing RED for ``(ticket, command)``.

        Refuses when the run's ``head_sha`` is not a full 40-char hex SHA, when
        its ``exit_code`` is 0 (a passing command is not a failing repro), or
        when a GREEN is already recorded for the same command (re-RED after GREEN
        is tamper). Idempotent on the ``(ticket, command_fingerprint)`` natural
        key: a rerun of the same failing command updates the RED fields in place.
        """
        clean_sha = _canonical_sha(run.head_sha)
        if not is_commit_sha(clean_sha):
            msg = (
                f"head_sha {run.head_sha!r} is not a full 40-char hex commit SHA (harness stamps `git rev-parse HEAD`)"
            )
            raise ReproEvidenceError(msg)
        if run.exit_code == 0:
            msg = (
                f"repro command exited 0 — a passing command is not a failing reproduction. "
                f"record-red requires the command to FAIL against the pre-fix tree {clean_sha[:8]}"
            )
            raise ReproEvidenceError(msg)
        fingerprint = _command_fingerprint(command)
        existing = cls.objects.filter(ticket=ticket, command_fingerprint=fingerprint).first()
        if existing is not None and existing.green_recorded_at is not None:
            msg = "a GREEN is already recorded for this command — re-recording RED after GREEN is tamper"
            raise ReproEvidenceError(msg)
        with transaction.atomic():
            row, _created = cls.objects.update_or_create(
                ticket=ticket,
                command_fingerprint=fingerprint,
                defaults={
                    "command": command.strip(),
                    "red_head_sha": clean_sha,
                    "red_exit_code": run.exit_code,
                    "red_output_digest": _output_digest(run.output),
                    "red_output_tail": run.output[-_OUTPUT_TAIL_MAX:],
                    "red_recorded_at": timezone.now(),
                    "provenance_ok": False,
                },
            )
            return row

    @classmethod
    def record_green(cls, *, ticket: Ticket, command: str, run: HarnessRun, red_is_ancestor: bool) -> "ReproEvidence":
        """Record the harness-run passing GREEN and freeze the provenance verdict.

        Refuses when the run's ``head_sha`` is not a full 40-char hex SHA, when
        its ``exit_code`` is non-zero (the fix did not fix it), when no matching
        RED exists, when the RED ran against the SAME tree as the GREEN
        (``red == green`` — the RED was captured with the fix), or when
        *red_is_ancestor* is False (the RED tree is not a proper ancestor of the
        GREEN tree — the provenance bypass). On success it writes the GREEN
        fields and stamps ``provenance_ok=True`` so the gate is a pure DB read.

        *red_is_ancestor* is computed by the CLI layer (``git merge-base
        --is-ancestor``) and passed in, so this domain factory stays free of
        git I/O and is exhaustively unit-testable.
        """
        clean_sha = _canonical_sha(run.head_sha)
        if not is_commit_sha(clean_sha):
            msg = (
                f"head_sha {run.head_sha!r} is not a full 40-char hex commit SHA (harness stamps `git rev-parse HEAD`)"
            )
            raise ReproEvidenceError(msg)
        if run.exit_code != 0:
            msg = (
                f"repro command exited {run.exit_code} — the fix did not fix it. record-green requires the SAME "
                f"command to PASS (exit 0) once the fix is applied at {clean_sha[:8]}"
            )
            raise ReproEvidenceError(msg)
        fingerprint = _command_fingerprint(command)
        row = cls.objects.filter(ticket=ticket, command_fingerprint=fingerprint).first()
        if row is None:
            msg = "no matching RED reproduction recorded for this command — run `repro record-red` first"
            raise ReproEvidenceError(msg)
        if row.red_head_sha == clean_sha:
            msg = (
                f"RED was captured against the SAME tree ({clean_sha[:8]}) as GREEN — the RED necessarily ran "
                f"WITH the fix present, so it proves nothing. The RED must be recorded before the fix commit"
            )
            raise ReproEvidenceError(msg)
        if not red_is_ancestor:
            msg = (
                f"the RED tree {row.red_head_sha[:8]} is not a proper ancestor of the GREEN tree {clean_sha[:8]} "
                f"— the fix was not applied on top of the RED tree, so the RED does not prove a pre-fix failure "
                f"(provenance bypass refused)"
            )
            raise ReproEvidenceError(msg)
        with transaction.atomic():
            row.green_head_sha = clean_sha
            row.green_exit_code = run.exit_code
            row.green_output_digest = _output_digest(run.output)
            row.green_output_tail = run.output[-_OUTPUT_TAIL_MAX:]
            row.green_recorded_at = timezone.now()
            row.provenance_ok = True
            row.save(
                update_fields=[
                    "green_head_sha",
                    "green_exit_code",
                    "green_output_digest",
                    "green_output_tail",
                    "green_recorded_at",
                    "provenance_ok",
                ]
            )
            return row
