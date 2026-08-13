"""``t3 teatree questions`` — manage the away-mode deferred-question backlog (#58).

Three subcommands operate on the durable :class:`DeferredQuestion` queue
populated when availability=away (BLUEPRINT §17.1 invariant 9):

* ``t3 teatree questions list`` — print pending questions, oldest first.
* ``t3 teatree questions answer <id> <answer>`` — resolve a question with a
    user answer; writes a :class:`DeferredQuestionAudit` row.
* ``t3 teatree questions dismiss <id> [--reason ...]`` — dismiss a question
    the user no longer wants to answer; writes an audit row.
* ``t3 teatree questions reachability`` — which automated resolvers can decide
    each pending row, and how many can be decided by none (#4178).
* ``t3 teatree questions resurface`` — re-post the pending backlog to the
    user's Slack DM (the away→present drain): returning from away never
    silently swallows questions. Reuses :func:`teatree.core.notify.notify_user`
    so each question is delivered at-most-once (idempotent ``BotPing``
    ledger) and routed through the per-overlay bot.

The list/answer/dismiss flow is the chat-only operator's parallel of
the interactive ``AskUserQuestion`` reply — the user resolves at their
own pace; the agent reads pending questions back via the same model
on its next turn.
"""

import io
from typing import IO, Annotated, TypedDict, cast

import typer
from django.db import transaction
from django_typer.management import command, initialize

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit, DeferredQuestionError
from teatree.core.models.task_handoff import schedule_resume
from teatree.core.notify_question_drains import drain_deferred_questions
from teatree.core.table_output import print_table


class DeferredQuestionRow(TypedDict):
    """One row of ``t3 <overlay> questions list --json``."""

    id: int
    status: str
    question: str
    created_at: str | None


class QuestionReachRow(TypedDict):
    """One row of ``t3 <overlay> questions reachability --json``."""

    id: int
    has_subject: bool
    resolvers: dict[str, str]


def _render_reachability_table(rows: list["QuestionReachRow"]) -> str:
    buffer = io.StringIO()
    unreachable = sum(1 for row in rows if not row["resolvers"])
    print_table(
        ["ID", "Subject", "Resolvers"],
        [
            [
                row["id"],
                "yes" if row["has_subject"] else "no",
                ", ".join(f"{name}={verdict}" for name, verdict in sorted(row["resolvers"].items())) or "—",
            ]
            for row in rows
        ],
        title=f"{len(rows)} pending, {unreachable} reachable by no resolver",
        stream=buffer,
        justify=["right", "left", "left"],
    )
    return buffer.getvalue()


def _render_questions_table(rows: list[DeferredQuestion]) -> str:
    buffer = io.StringIO()
    table_rows = [
        [row.pk, row.status, row.created_at.isoformat() if row.created_at is not None else "?", row.question]
        for row in rows
    ]
    print_table(
        ["ID", "Status", "Created", "Question"],
        table_rows,
        title=f"{len(rows)} deferred question(s)",
        stream=buffer,
        justify=["right", "left", "left", "left"],
    )
    return buffer.getvalue()


class Command(MachineOutputCommand):
    @initialize()
    def init(self) -> None:
        """``t3 teatree questions`` group root."""

    @command()
    def record(
        self,
        question: Annotated[str, typer.Argument(help="The question text.")],
        *,
        options_json: Annotated[
            str,
            typer.Option("--options", help="Verbatim JSON-encoded ``AskUserQuestion`` options."),
        ] = "",
        session_id: Annotated[str, typer.Option("--session", help="Originating session id.")] = "",
        dedupe_marker: Annotated[
            str,
            typer.Option(
                "--dedupe-marker",
                help="Escalate-once scope; an open question already carrying it is returned unchanged.",
            ),
        ] = "",
        audience: Annotated[
            str,
            typer.Option("--audience", help="owner_question (DM'd to the owner) or internal (logged only)."),
        ] = DeferredQuestion.Audience.OWNER_QUESTION,
    ) -> str:
        """Record a deferred question by hand — the agent-facing capture surface.

        ``--dedupe-marker`` and ``--audience`` are the two columns the scanners
        already set, exposed so a question recorded here carries the SAME shape:
        its row collapses onto the scanner's row for one underlying signal, and
        an agent's self-report about its own tooling can be marked internal
        instead of reaching the owner's DM.

        There is no ``--tool-use-id``: that identifier is assigned by the harness
        and nobody at a shell can know it. The away-mode ``AskUserQuestion``
        PreToolUse hook records its own rows through
        :meth:`DeferredQuestion.record` directly and sets it there.
        """
        if audience not in DeferredQuestion.Audience.values:
            self.stderr.write(f"unknown audience {audience!r} — expected one of {DeferredQuestion.Audience.values}")
            raise SystemExit(2)
        try:
            row = DeferredQuestion.record(
                question,
                options_json=options_json,
                session_id=session_id,
                dedupe_marker=dedupe_marker,
                audience=audience,
            )
        except DeferredQuestionError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(2) from exc
        return f"recorded #{row.pk}."

    @command(name="list")
    def list_pending(
        self,
        *,
        all_rows: Annotated[
            bool,
            typer.Option("--all/--pending", help="Include answered/dismissed rows."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the deferred questions as JSON instead of the human view."),
        ] = False,
    ) -> list[DeferredQuestionRow]:
        """List pending deferred questions, oldest first."""
        rows = list(DeferredQuestion.objects.order_by("-created_at")) if all_rows else list(DeferredQuestion.pending())
        payload: list[DeferredQuestionRow] = [
            {
                "id": row.pk,
                "status": row.status,
                "question": row.question,
                "created_at": row.created_at.isoformat() if row.created_at is not None else None,
            }
            for row in rows
        ]
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render_questions_table(rows) if rows else "no deferred questions.",
        )
        return payload

    @command()
    def reachability(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the reachability rows as JSON instead of the human view."),
        ] = False,
    ) -> list[QuestionReachRow]:
        """Report which automated resolvers can decide each pending question (#4178).

        A row with no resolver is one only a human can ever clear — the gap that let
        the backlog reach 70 pending with a single automated drain covering 6.
        """
        from teatree.loop.question_drain import question_reachability  # noqa: PLC0415 — loop layer, not a model import

        payload: list[QuestionReachRow] = [
            {
                "id": reach.question_id,
                "has_subject": reach.has_subject,
                "resolvers": {name: str(verdict) for name, verdict in reach.decisions.items()},
            }
            for reach in question_reachability()
        ]
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render_reachability_table(payload) if payload else "no pending questions.",
        )
        return payload

    @command()
    def answer(
        self,
        question_id: int,
        text: Annotated[str, typer.Argument(help="The user's answer.")],
        resolver_id: Annotated[
            str,
            typer.Option("--resolver", help="Identity of the resolver (audit trail)."),
        ] = "",
        also: Annotated[
            list[int] | None,
            typer.Option("--also", help="Another question this same answer resolves (repeatable)."),
        ] = None,
    ) -> str:
        """Resolve pending questions with a user answer (resumes any parked headless task).

        ``--also`` exists because one decision routinely settles several questions:
        a loop that cannot act on an ambiguous instruction files a clarifying question
        per facet, and the operator answers all of them with one sentence. Retyping
        that sentence per id costs a container round-trip each and invites the answers
        to drift apart in wording, which later reads as four different decisions.

        The id stays positional so ``answer <id> <text>`` is unchanged; the extra ids
        are options because a greedy list would swallow the answer text.

        Each id is consumed in its OWN transaction, so one already-resolved id skips
        rather than rolling back the rest.
        """
        if not text.strip():
            self.stderr.write("answer text must not be empty")
            raise SystemExit(2)
        answered: list[int] = []
        skipped: list[int] = []
        for target in [question_id, *(also or [])]:
            try:
                with transaction.atomic():
                    row = DeferredQuestion.consume(target, answer=text)
                    if row is None:
                        skipped.append(target)
                        continue
                    row.resolved_via = DeferredQuestion.ResolvedVia.LOCAL
                    row.save(update_fields=["resolved_via"])
                    DeferredQuestionAudit.objects.create(
                        question=row,
                        action="answered",
                        answer_text=text,
                        resolver_id=resolver_id,
                    )
                    if row.parked_task is not None:
                        schedule_resume(row.parked_task, answer=text)
                    answered.append(row.pk)
            except DeferredQuestionError as exc:
                self.stderr.write(str(exc))
                raise SystemExit(2) from exc
        if skipped:
            self.stderr.write(f"not found or already resolved: {', '.join(str(i) for i in skipped)}")
        if not answered:
            raise SystemExit(1)
        return f"answered {len(answered)}: {', '.join(f'#{pk}' for pk in answered)}."

    @command()
    def dismiss(
        self,
        question_ids: Annotated[
            list[int],
            typer.Argument(help="One or more question ids to dismiss with the same reason."),
        ],
        reason: Annotated[
            str,
            typer.Option("--reason", help="Why the question is being dropped (audit trail)."),
        ] = "no longer relevant",
        resolver_id: Annotated[
            str,
            typer.Option("--resolver", help="Identity of the resolver (audit trail)."),
        ] = "",
    ) -> str:
        """Dismiss pending questions without answering them.

        Takes MANY ids because a backlog is dismissed by the class, not one at a
        time. A containerized invocation costs a round-trip of its own, so clearing
        ninety questions one command each is hours of wall clock — long enough that
        the sweep gets abandoned half-done, which is how a queue of automated halts
        buries the handful of questions a human actually needs to answer.

        Each id is consumed in its OWN transaction, so an id that is already resolved
        skips instead of rolling back the ids around it — partial success is the
        normal outcome of a sweep, not a failure of one.
        """
        clean_reason = reason.strip() or "no longer relevant"
        dismissed: list[int] = []
        skipped: list[int] = []
        for question_id in question_ids:
            try:
                with transaction.atomic():
                    row = DeferredQuestion.consume(question_id, dismissed_reason=clean_reason)
                    if row is None:
                        skipped.append(question_id)
                        continue
                    row.resolved_via = DeferredQuestion.ResolvedVia.LOCAL
                    row.save(update_fields=["resolved_via"])
                    DeferredQuestionAudit.objects.create(
                        question=row,
                        action="dismissed",
                        dismissed_reason=clean_reason,
                        resolver_id=resolver_id,
                    )
                    dismissed.append(row.pk)
            except DeferredQuestionError as exc:
                self.stderr.write(str(exc))
                raise SystemExit(2) from exc
        if skipped:
            self.stderr.write(f"not found or already resolved: {', '.join(str(i) for i in skipped)}")
        if not dismissed:
            raise SystemExit(1)
        return f"dismissed {len(dismissed)}: {', '.join(f'#{pk}' for pk in dismissed)}."

    @command()
    def resurface(
        self,
        user_id: Annotated[
            str,
            typer.Option("--user-id", help="Slack user id to DM (defaults to the configured user)."),
        ] = "",
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay bot routing)."),
        ] = "",
    ) -> str:
        """Re-post the pending backlog to the user's Slack DM (away→present drain).

        Manual / idempotent entry point to the same
        :func:`teatree.core.notify_question_drains.drain_deferred_questions` egress the
        ``write_override(MODE_PRESENT)`` away→present transition auto-fires,
        so a re-run never double-posts (the ``BotPing`` ledger dedupes).
        """
        delivered, total = drain_deferred_questions(user_id=user_id, overlay=overlay)
        if total == 0:
            return "no pending questions to resurface."
        if delivered == 0:
            return (
                f"resurfaced nothing new — {total} question(s) still pending. Either none was newly "
                f"selected, or no attempted send landed; check the BotPing ledger for which."
            )
        # Name what is left, not just what went out. `resurfaced 3/3` was read as an empty
        # queue while 69 were outstanding — the number that matters to the reader is the
        # remainder (#4064).
        remaining = max(0, total - delivered)
        return f"resurfaced {delivered} new question(s); {remaining} of {total} still pending."
