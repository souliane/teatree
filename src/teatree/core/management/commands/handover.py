"""``t3 <overlay> handover`` — hand all current work to another session.

Reuses the PreCompact durable-state snapshot as the hand-off payload and
the ``t3-master`` slot for the default target. ``create`` persists a
:class:`~teatree.core.models.SessionHandover` row (the delivery surface),
mirrors it to the XDG file, runs the sub-agent barrier and folds its returns
into the persisted payload, then re-reads the row and asserts it complete
before reporting anything — unless the payload resolves EMPTY, in which case
the barrier still runs and NO row is written; ``whoami`` prints this session's id;
``claim-on-start`` is the SessionStart-hook entry point that atomically
claims an unclaimed hand-off for a starting session and returns its payload.

ORM access is here (a management command, not a plain typer command) per
the project's "anything touching the ORM is a management command" rule.
"""

import sys
from pathlib import Path
from typing import IO, Annotated, NoReturn, cast

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.core.handover import (
    PayloadSource,
    SelfAddressedHandoverError,
    claim_handovers,
    create_handover,
    resolve_handover,
)
from teatree.core.handover_orchestration import SubagentPush, drive_subagents_to_fast_push
from teatree.core.handover_wrapup import (
    SUBAGENT_MARKER_START,
    merge_subagent_records,
    subagent_record,
    upsert_subagent_section,
)
from teatree.core.machine_output import emit
from teatree.core.models import SessionHandover
from teatree.core.session_identity import is_loop_runner_session
from teatree.loop.session_identity import current_session_id


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
        resolution = resolve_handover(from_session=from_session, explicit_to=to, authored=authored)

        # The barrier runs BEFORE the write on every path, including the refused one:
        # rescuing a sub-agent's unpushed work is orthogonal to whether this session
        # has a payload, and a session with nothing to hand over is the profile most
        # likely to be stranding some. Do not "restore" persist-first — an EMPTY
        # resolve has no state to protect, only a dead row to avoid writing.
        pushes = self._drive_subagents() if drive_subagents else []
        if resolution.resolved.source is PayloadSource.EMPTY:
            self._refuse_empty(from_session, pushes=pushes, json_output=json_output)

        try:
            created = create_handover(from_session=from_session, explicit_to=to, authored=authored)
        except SelfAddressedHandoverError as exc:
            self._refuse(str(exc), json_output=json_output, code=1)

        handover, mirror, source = created.handover, created.mirror, created.source
        recipient = handover.to_session or "next-session"
        if drive_subagents:
            mirror = self._fold_subagent_wrapup(handover, pushes)
        failures = self._completeness_failures(
            pk=handover.pk, expected=created.resolved, drove_subagents=drive_subagents
        )
        # A hand-off that reports OK while transferring nothing usable is worse
        # than one that fails: the operator moves on believing state was carried
        # over, and the receiving session claims a row that does not hold it
        # (#3551, #3888). Only a VETTED source whose row survives the re-read may
        # report OK, and the re-read happens BEFORE the line is written.
        ok = source.is_vetted and not failures
        status = "ERROR" if failures else ("OK   " if source.is_vetted else "WARN ")
        human_lines = [
            f"{status} hand-off #{handover.pk} handed off to {recipient} ({source.value}); mirror written to {mirror}."
        ]
        if created.updated_existing:
            human_lines.append(
                f"WARN  absorbed into this session's existing unclaimed hand-off, which already carried "
                f"{created.previous_bytes} bytes — one row per session, and nothing it held was dropped."
            )
        human_lines += [f"      sub-agent {push.branch}: {self._push_summary(push)}" for push in pushes]
        human_lines += [f"ERROR completeness: {failure}" for failure in failures]
        emit(
            {
                "ok": ok,
                "handover_id": handover.pk,
                "payload_source": source.value,
                "empty_payload": False,
                "row_written": True,
                "from_session": handover.from_session,
                "to_session": handover.to_session,
                "parked_for_next": handover.is_for_next_session,
                "mirror_path": str(mirror),
                "updated_existing": created.updated_existing,
                "previous_payload_bytes": created.previous_bytes,
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
        if not source.is_vetted:
            self.stderr.write(
                f"WARN  hand-off {handover.pk} to {recipient} is UNVETTED: no authored body and no PreCompact "
                f"snapshot for session {from_session}, so its payload was DERIVED from the DB. It carries the "
                f"in-flight inventory and NONE of this session's reasoning. Re-run with "
                f"`--from-file <path>` (or `--body`) to hand over what this session actually knows."
            )
            raise SystemExit(3)

    @staticmethod
    def _fold_subagent_wrapup(handover: SessionHandover, pushes: list[SubagentPush]) -> Path:
        """Merge this barrier's returns into the row's union and re-render its one block.

        The union is what makes a second hand-off UPDATE the wrap-up rather than
        append a second one, while still naming an agent this barrier no longer sees.
        """
        now = timezone.now()
        records = merge_subagent_records(handover.subagent_wrapup, [subagent_record(push, at=now) for push in pushes])
        return upsert_subagent_section(handover, records)

    @staticmethod
    def _completeness_failures(*, pk: int, expected: str, drove_subagents: bool) -> list[str]:
        """What is wrong with the PERSISTED row, read fresh from the DB — ``[]`` when nothing is.

        Verify-by-re-read: the in-memory row is what the command believes it wrote,
        so checking it proves only that the command agrees with itself. Every check
        here runs against a fresh fetch, before any success line is written.
        """
        row = SessionHandover.objects.get(pk=pk)
        unclaimed = SessionHandover.objects.filter(from_session=row.from_session, claimed_at__isnull=True).count()
        sections = row.payload.count(SUBAGENT_MARKER_START)
        checks = (
            (isinstance(row.pk, int) and row.pk > 0, f"the row id {row.pk!r} is not a positive integer"),
            (bool(row.payload.strip()), "the persisted payload is empty"),
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
            (
                not drove_subagents or sections == 1,
                "the sub-agent barrier ran but its per-agent returns are not in the payload",
            ),
            # Verify-by-re-read must catch THIS bug class too: the wrap-up used to be
            # appended, so N hand-offs left N sections. An authored body that itself
            # quotes the marker trips this loudly rather than silently — loud-over-
            # silent is the intended polarity.
            (
                sections <= 1,
                f"the payload carries {sections} sub-agent wrap-up sections — exactly one is allowed",
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

    def _drive_subagents(self) -> list[SubagentPush]:
        """Fast-push in-flight sub-agent worktrees; a failure here never fails the hand-off.

        The hand-off row + mirror are already durable, so the orchestration
        step is best-effort: a git/network hiccup is logged and swallowed
        rather than losing the recorded hand-off.
        """
        cwd = Path.cwd()
        try:
            return drive_subagents_to_fast_push(str(cwd), exclude=(cwd,))
        except Exception:  # noqa: BLE001 — the hand-off is already persisted; sub-agent driving must not fail it
            self.stderr.write(f"WARN  could not drive sub-agents to fast-push from {cwd} (hand-off still recorded).")
            return []

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
