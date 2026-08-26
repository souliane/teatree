"""``t3 <overlay> handover`` — hand all current work to another session.

Reuses the PreCompact durable-state snapshot as the hand-off payload and
the ``t3-master`` slot for the default target. ``create`` persists a
:class:`~teatree.core.models.SessionHandover` row (the delivery surface),
mirrors it to the XDG file, runs the sub-agent barrier and records its returns
as ROW STATE — never editing the payload, which the barrier would otherwise have
to read to find "its own" bytes — then re-reads the row and asserts it complete
before reporting anything — unless the payload resolves EMPTY, in which case
the barrier still runs and NO row is written — a refusal decided by the SAME
resolution the write uses, taken once after the barrier, since live state can
settle across it. ``whoami`` prints this session's id;
``claim-on-start`` is the SessionStart-hook entry point that atomically
claims an unclaimed hand-off for a starting session and returns its payload.

ORM access is here (a management command, not a plain typer command) per
the project's "anything touching the ORM is a management command" rule.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Annotated, NoReturn, cast

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.core.handover import (
    CreatedHandover,
    PayloadSource,
    SelfAddressedHandoverError,
    claim_handovers,
    create_handover,
    dangling_backlog_claims,
    resolve_handover,
    write_mirror,
)
from teatree.core.handover_orchestration import SubagentPush, drive_subagents_to_fast_push
from teatree.core.handover_wrapup import SubagentRecord, merge_subagent_records, record_barrier_returns, subagent_record
from teatree.core.machine_output import emit
from teatree.core.models import SessionHandover
from teatree.core.session_identity import is_loop_runner_session
from teatree.loop.session_identity import current_session_id
from teatree.utils.git import run


@dataclass(frozen=True, slots=True)
class _RecordedBarrier:
    """What recording the barrier's returns DID: the union, the re-mirror, and the re-read row.

    ``row`` is the ONE fresh fetch taken after the write. Both the completeness checks
    and the flags the command reports read it, so a row that vanished between the two
    cannot be reported present by one and missing by the other.
    """

    records: list[SubagentRecord]
    mirror: Path
    row: "SessionHandover | None"

    @property
    def payload_is_empty(self) -> bool:
        """Whether the PERSISTED payload holds nothing — ``True`` when the row is gone."""
        return not (self.row.payload.strip() if self.row else "")

    @property
    def last_barrier_at(self) -> str | None:
        """When a barrier last completed on the persisted row, ISO-formatted, or ``None``."""
        return self.row.last_barrier_at.isoformat() if self.row and self.row.last_barrier_at else None


class Command(TyperCommand):
    help = "Hand all current work from this session to another session."

    @initialize()
    def init(self) -> None:
        """``t3 <overlay> handover`` group root."""

    @command()
    def create(
        self,
        *,
        to: Annotated[
            str,
            typer.Option("--to", help="Target session id. Omit to hand to the live loop owner, else park for next."),
        ] = "",
        from_file: Annotated[
            str,
            typer.Option("--from-file", help="Read the hand-off body from this file ('-' for stdin)."),
        ] = "",
        body: Annotated[
            str,
            typer.Option("--body", help="The hand-off body, given inline."),
        ] = "",
        drive_subagents: Annotated[
            bool,
            typer.Option(
                "--drive-subagents/--no-drive-subagents",
                help="Fast-push in-flight sub-agent worktrees before they are terminated (directive #8).",
            ),
        ] = True,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Hand this session's full durable state to another session.

        ``--from-file`` / ``--body`` supply the payload the session AUTHORED —
        the reasoning no query can re-derive, and the reason a hand-off exists.
        Without one, the payload falls back to this session's PreCompact snapshot
        and then to live DB state, and only the first two report ``OK``: a
        machine-derived inventory nobody vetted is recorded and reported UNVETTED,
        because "hand-off written" over a payload the receiver cannot use is the
        failure this command is supposed to make impossible.

        No ``--to`` → the live ``t3-master`` slot holder; if none, parked
        for whichever session starts next. Per directive #8, every in-flight
        sub-agent worktree is driven through leak-gated fast-push so their work is
        committed/pushed/PR'd BEFORE the orchestrator terminates them — and that
        barrier runs on the refused path too, since a session with nothing to hand
        over is the one most likely to be stranding a sub-agent's work.

        A resolve that finds NOTHING writes nothing: no row, no mirror, and no
        mutation of this author's existing unclaimed row.
        """
        from_session = current_session_id()
        if not from_session:
            msg = "no Claude session id — run inside a Claude Code session to hand off its state"
            self._refuse(msg, json_output=json_output, code=2)

        authored = self._read_authored(from_file=from_file, body=body, json_output=json_output)

        # The barrier runs BEFORE the write on every path, including the refused one:
        # rescuing a sub-agent's unpushed work is orthogonal to whether this session
        # has a payload, and a session with nothing to hand over is the profile most
        # likely to be stranding some. Do not "restore" persist-first — an EMPTY
        # resolve has no state to protect, only a dead row to avoid writing.
        # Resolve AFTER it, ONCE, and thread that answer into the write: the barrier is
        # long enough for the last ticket to settle or the snapshot to rotate underneath
        # it, so a gate decided on one side of it cannot speak for a write on the other.
        barrier_ran, pushes = self._drive_subagents(enabled=drive_subagents)
        resolution = resolve_handover(from_session=from_session, explicit_to=to, authored=authored)
        if resolution.resolved.source is PayloadSource.EMPTY:
            self._refuse_empty(from_session, pushes=pushes, json_output=json_output)

        try:
            created = create_handover(from_session=from_session, resolution=resolution)
        except SelfAddressedHandoverError as exc:
            self._refuse(str(exc), json_output=json_output, code=1)

        handover, source = created.handover, created.source
        recipient = handover.to_session or "next-session"
        recorded = self._record_barrier(handover, pushes, barrier_ran=barrier_ran)
        failures = self._completeness_failures(
            row=recorded.row, expected=created.resolved, records=recorded.records, barrier_ran=barrier_ran
        )
        # A hand-off that reports OK while transferring nothing usable is worse
        # than one that fails: the operator moves on believing state was carried
        # over, and the receiving session claims a row that does not hold it
        # (#3551, #3888). Only a VETTED source whose row survives the re-read may
        # report OK, and the re-read happens BEFORE the line is written.
        dangling = dangling_backlog_claims(str(handover.payload))
        ok = source.is_vetted and not failures
        status = "ERROR" if failures else ("OK   " if source.is_vetted else "WARN ")
        human_lines = [
            (
                f"{status} hand-off #{handover.pk} handed off to {recipient} ({source.value}); "
                f"mirror written to {recorded.mirror}."
            )
        ]
        if created.updated_existing:
            human_lines.append(self._absorb_note(created))
        human_lines += [f"      sub-agent {push.branch}: {self._push_summary(push)}" for push in pushes]
        human_lines += [f"ERROR completeness: {failure}" for failure in failures]
        emit(
            {
                "ok": ok,
                "handover_id": handover.pk,
                "payload_source": source.value,
                "dangling_backlog_claims": dangling,
                # Derived from the same re-read the completeness checks ran against, so the
                # pair that once read `empty` beside `row_written: true` cannot recur by
                # asserting itself: a row that vanished between the write and the read
                # reports `row_written: false`, and the checks fail alongside it.
                "empty_payload": recorded.payload_is_empty,
                "row_written": recorded.row is not None,
                "barrier_ran": barrier_ran,
                "last_barrier_at": recorded.last_barrier_at,
                "from_session": handover.from_session,
                "to_session": handover.to_session,
                "parked_for_next": handover.is_for_next_session,
                "mirror_path": str(recorded.mirror),
                "updated_existing": created.updated_existing,
                "previous_payload_bytes": created.previous_bytes,
                # False when the row already held these bytes: a duplicate drop, which
                # otherwise reads exactly like an append that added nothing.
                "payload_appended": created.payload_appended,
                "payload_bytes": len(handover.payload),
                "subagent_count": len(pushes),
                "subagent_pushes": [self._push_json(push) for push in pushes],
                "completeness_ok": not failures,
                "completeness_failures": failures,
            },
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(human_lines),
        )
        if failures:
            for failure in failures:
                self.stderr.write(f"ERROR hand-off {handover.pk} is INCOMPLETE: {failure}")
            raise SystemExit(1)
        if dangling:
            self.stderr.write(
                f"WARN  hand-off {handover.pk} counts a backlog it never locates: {', '.join(dangling)}. "
                f"A receiver cannot expand a number — and a per-session task list dies with the session that "
                f"held it. Name the durable home (a file path or a URL) in the same paragraph as the count, or "
                f"drop the count."
            )
        if not source.is_vetted:
            self.stderr.write(
                f"WARN  hand-off {handover.pk} to {recipient} is UNVETTED: no authored body and no PreCompact "
                f"snapshot for session {from_session}, so its payload was DERIVED from the DB. It carries the "
                f"in-flight inventory and NONE of this session's reasoning. Re-run with "
                f"`--from-file <path>` (or `--body`) to hand over what this session actually knows."
            )
            raise SystemExit(3)

    @staticmethod
    def _absorb_note(created: CreatedHandover) -> str:
        """How much state the absorbed-into row already held, and whether these bytes added to it.

        A payload the row already carried is dropped as a duplicate rather than repeated.
        The receiver gets those bytes either way, but the JSON otherwise reads exactly like
        an append that happened to add nothing.
        """
        landed = "appended behind a fence" if created.payload_appended else "ALREADY PRESENT, so nothing was added"
        return (
            f"WARN  absorbed into this session's existing unclaimed hand-off, which already carried "
            f"{created.previous_bytes} bytes — one row per session, and nothing it held was dropped. "
            f"This hand-off's bytes were {landed}."
        )

    @staticmethod
    def _record_barrier(
        handover: SessionHandover, pushes: list[SubagentPush], *, barrier_ran: bool
    ) -> _RecordedBarrier:
        """Merge this barrier's returns into the row's union, record the fact, re-mirror, re-read.

        Unconditional, on the barrier-less path too: ``in_latest_barrier`` is a freshness
        claim about THIS hand-off, so leaving the union untouched keeps asserting that a
        barrier which never ran enumerated its agents. Merging against zero returns flips
        them to NOT-enumerated and records ``barrier_ran=False``, which is the true
        statement. There is no branch left to forge, because nothing here reads the payload.

        ``unique_mirror_path`` keys on ``created_at``, which none of this touches, so the
        re-mirror OVERWRITES the same file — one hand-off stays one file, no pointer churn.
        """
        now = timezone.now()
        records = merge_subagent_records(handover.subagent_wrapup, [subagent_record(p, at=now) for p in pushes])
        record_barrier_returns(handover, records, at=now, barrier_ran=barrier_ran)
        return _RecordedBarrier(
            records=records,
            mirror=write_mirror(handover),
            row=SessionHandover.objects.filter(pk=handover.pk).first(),
        )

    @staticmethod
    def _completeness_failures(
        *, row: SessionHandover | None, expected: str, records: list[SubagentRecord], barrier_ran: bool
    ) -> list[str]:
        """What is wrong with the PERSISTED *row* — ``[]`` when nothing is.

        Verify-by-re-read: the in-memory row is what the command believes it wrote, so
        checking it proves only that the command agrees with itself. *row* is the
        caller's single fresh fetch, taken before any success line is written and shared
        with the flags the command reports, so the two can never disagree.
        """
        if row is None:
            return ["the row is gone from the DB — nothing was persisted"]
        unclaimed = SessionHandover.objects.filter(from_session=row.from_session, claimed_at__isnull=True).count()
        checks = (
            (isinstance(row.pk, int) and row.pk > 0, f"the row id {row.pk!r} is not a positive integer"),
            (bool(row.payload.strip()), "the persisted payload is empty"),
            # With nothing splicing the payload this is the MAJOR-2 guard: it can only
            # fail if something destroyed the bytes the author handed over.
            (
                not expected.strip() or expected.strip() in row.payload,
                "the persisted payload does not carry the bytes this hand-off resolved",
            ),
            (row.claimed_at is None, f"the row was already claimed by {row.claimed_by!r} — nothing to deliver"),
            (
                not row.to_session or row.to_session != row.from_session,
                "the row is addressed to its own author, so no session can claim it",
            ),
            (
                not is_loop_runner_session(row.to_session),
                f"the row is addressed to {row.to_session!r}, an id no receiving session can have",
            ),
            # The wrap-up is checked as ROW STATE, which is strictly stronger than counting
            # markers in text: the union either equals what this hand-off merged or it does
            # not, and no authored body can make either answer come out differently.
            (
                row.subagent_wrapup == list(records),
                "the persisted sub-agent wrap-up is not the union this hand-off merged",
            ),
            (
                row.barrier_ran_at_latest_handoff == barrier_ran,
                "the row does not record whether this hand-off ran a sub-agent barrier",
            ),
            (
                not barrier_ran or row.last_barrier_at is not None,
                "a sub-agent barrier ran but the row records none",
            ),
            (unclaimed == 1, f"this session holds {unclaimed} unclaimed hand-offs — exactly one is allowed"),
        )
        return [failure for held, failure in checks if not held]

    def _refuse_empty(self, from_session: str, *, pushes: list[SubagentPush], json_output: bool) -> "NoReturn":
        """Refuse a hand-off with nothing to transfer — writing NO row, NO mirror, nothing.

        A zero-agent barrier result is a negative fact ABOUT a hand-off, not a
        hand-off: it exists so that, within a row that carries state, an absent
        wrap-up cannot be mistaken for a barrier that never ran. On its own it
        transfers nothing a receiver can act on, yet a row carrying it arrives under
        the ``SESSION HAND-OFF RECEIVED`` directive and consumes this author's single
        unclaimed slot, so the next REAL hand-off absorbs behind it.

        The barrier's own returns still travel — on stdout, stderr and in the JSON —
        because the rescue happened whether or not a payload existed.
        """
        human_lines = ["ERROR hand-off REFUSED — no durable state to hand over; NO row was written."]
        human_lines += [f"      sub-agent {push.branch}: {self._push_summary(push)}" for push in pushes]
        emit(
            {
                "ok": False,
                "handover_id": None,
                "payload_source": PayloadSource.EMPTY.value,
                "empty_payload": True,
                "row_written": False,
                "mirror_path": "",
                "subagent_count": len(pushes),
                "subagent_pushes": [self._push_json(push) for push in pushes],
            },
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(human_lines),
        )
        self.stderr.write(
            f"ERROR hand-off carries NO durable state — no authored body, no PreCompact snapshot "
            f"for session {from_session}, and no in-flight worktrees, tickets or PRs to derive one from. "
            f"No row was written, so nothing will be delivered."
        )
        raise SystemExit(1)

    def _refuse(self, msg: str, *, json_output: bool, code: int) -> "NoReturn":
        """Report a refusal on both channels and exit non-zero — never a silent no-op."""
        emit(
            {"ok": False, "error": msg},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"ERROR  {msg}",
        )
        self.stderr.write(f"ERROR {msg}")
        raise SystemExit(code)

    def _read_authored(self, *, from_file: str, body: str, json_output: bool) -> str:
        """The authored payload from ``--from-file`` / ``--body``, or ``""`` when neither was given.

        An unreadable ``--from-file`` REFUSES rather than degrading to a derived
        payload: the session asked for specific bytes to be handed over, and
        silently handing over different ones is the defect, not a fallback.
        """
        if from_file and body:
            self._refuse(
                "--from-file and --body both given — pass exactly one, so the payload has one author",
                json_output=json_output,
                code=2,
            )
        if body:
            return body
        if not from_file:
            return ""
        if from_file == "-":
            return sys.stdin.read()
        try:
            return Path(from_file).read_text(encoding="utf-8")
        except OSError as exc:
            self._refuse(f"could not read --from-file {from_file}: {exc}", json_output=json_output, code=2)

    def _drive_subagents(self, *, enabled: bool) -> tuple[bool, list[SubagentPush]]:
        """Fast-push in-flight sub-agent worktrees; a failure here never fails the hand-off.

        Returns ``(completed, pushes)``. ``(False, [])`` when the barrier was SKIPPED or
        RAISED, which the empty list alone cannot distinguish from "ran and found none" —
        recording a crash as a clean sweep is how the row comes to claim a barrier that
        never finished.

        The hand-off row + mirror are already durable, so the orchestration step is
        best-effort: a git/network hiccup is logged and swallowed rather than losing the
        recorded hand-off.
        """
        if not enabled:
            return False, []
        cwd = Path.cwd()
        try:
            return True, drive_subagents_to_fast_push(str(cwd), exclude=self._own_worktree_roots(cwd))
        except Exception:  # noqa: BLE001 — the hand-off is already persisted; sub-agent driving must not fail it
            self.stderr.write(f"WARN  could not drive sub-agents to fast-push from {cwd} (hand-off still recorded).")
            return False, []

    @staticmethod
    def _own_worktree_roots(cwd: Path) -> tuple[Path, ...]:
        """*cwd* AND the root of the checkout containing it — the barrier's exclusion set.

        ``in_flight_subagent_worktrees`` matches a worktree by its resolved ROOT, so
        excluding only *cwd* misses the agent's own worktree whenever ``handover
        create`` runs from a subdirectory of it — and the barrier then fast-pushes the
        very checkout it is running in. Latent before the jobs-dir widening made
        ``.claude/jobs/<session>/**`` enumerable; reachable after it.

        ``git.run`` returns ``""`` on failure, so a cwd inside no repository degrades
        to excluding *cwd* alone.
        """
        toplevel = run(repo=str(cwd), args=["rev-parse", "--show-toplevel"]).strip()
        return (cwd, Path(toplevel)) if toplevel else (cwd,)

    @staticmethod
    def _push_json(push: SubagentPush) -> dict[str, object]:
        outcome = push.outcome
        return {
            "worktree": str(push.worktree),
            "branch": push.branch,
            "driven": push.driven,
            "committed": bool(outcome and outcome.committed),
            "pushed": bool(outcome and outcome.pushed),
            "pr_url": outcome.pr_url if outcome else "",
            "error": push.error,
        }

    @staticmethod
    def _push_summary(push: SubagentPush) -> str:
        if not push.driven:
            return f"NOT pushed ({push.error or 'unknown error'})"
        outcome = push.outcome
        if outcome is None or not outcome.ok:
            findings = "; ".join(f.detail for f in outcome.findings) if outcome else "no outcome"
            return f"REFUSED ({findings})"
        pr = f" PR {outcome.pr_url}" if outcome.pr_url else ""
        return f"pushed (committed={outcome.committed}){pr}"

    @command()
    def whoami(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Print this Claude session's own id (the hand-off ``--to`` target)."""
        session_id = current_session_id()
        emit(
            {"session_id": session_id},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=session_id or "(no Claude session id — not running inside a Claude Code session)",
        )

    @command(name="claim-on-start")
    def claim_on_start(
        self,
        *,
        session: Annotated[str, typer.Option("--session", help="The starting session id claiming a hand-off.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = True,
    ) -> None:
        """Atomically claim an unclaimed hand-off for *session* and print its payload.

        The SessionStart hook calls this for a fresh / non-owner session: it
        claims a hand-off targeted AT the session (preferred) or parked for
        "next session", marks it claimed so it injects exactly once, and
        prints the payload. Empty payload when nothing is claimable.
        """
        payload, origin = claim_handovers(session or current_session_id())
        emit(
            {"claimed": bool(payload), "from_session": origin, "payload": payload},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=payload or None,
        )
