"""``t3 teatree questions`` — manage the away-mode deferred-question backlog (#58).

Three subcommands operate on the durable :class:`DeferredQuestion` queue
populated when availability=away (BLUEPRINT §17.1 invariant 9):

* ``t3 teatree questions list`` — print pending questions, oldest first.
* ``t3 teatree questions answer <id> <answer>`` — resolve a question with a
    user answer; writes a :class:`DeferredQuestionAudit` row.
* ``t3 teatree questions dismiss <id> [--reason ...]`` — dismiss a question
    the user no longer wants to answer; writes an audit row.
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

import json
from typing import Annotated

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit, DeferredQuestionError
from teatree.core.models.task_handoff import schedule_headless_resume
from teatree.core.notify_question_drains import drain_deferred_questions


def _format_row(row: DeferredQuestion) -> str:
    age = row.created_at.isoformat() if row.created_at is not None else "?"
    return f"  #{row.pk} [{row.status}] {age}\n     {row.question}"


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 teatree questions`` group root."""

    @command()
    def record(
        self,
        question: Annotated[str, typer.Argument(help="The question text.")],
        options_json: Annotated[
            str,
            typer.Option("--options", help="Verbatim JSON-encoded ``AskUserQuestion`` options."),
        ] = "",
        session_id: Annotated[str, typer.Option("--session", help="Originating session id.")] = "",
        tool_use_id: Annotated[str, typer.Option("--tool-use-id", help="Originating tool_use id.")] = "",
    ) -> str:
        """Record a deferred question (called by the PreToolUse away-mode hook)."""
        try:
            row = DeferredQuestion.record(
                question,
                options_json=options_json,
                session_id=session_id,
                tool_use_id=tool_use_id,
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
    ) -> str:
        """List pending deferred questions, oldest first."""
        rows = list(DeferredQuestion.objects.order_by("-created_at")) if all_rows else list(DeferredQuestion.pending())
        if json_output:
            return json.dumps(
                [
                    {
                        "id": row.pk,
                        "status": row.status,
                        "question": row.question,
                        "created_at": row.created_at.isoformat() if row.created_at is not None else None,
                    }
                    for row in rows
                ]
            )
        if not rows:
            return "no deferred questions."
        lines = [f"{len(rows)} deferred question(s):"]
        lines.extend(_format_row(row) for row in rows)
        return "\n".join(lines)

    @command()
    def answer(
        self,
        question_id: int,
        text: Annotated[str, typer.Argument(help="The user's answer.")],
        resolver_id: Annotated[
            str,
            typer.Option("--resolver", help="Identity of the resolver (audit trail)."),
        ] = "",
    ) -> str:
        """Resolve a pending question with a user answer (resumes a parked headless task)."""
        if not text.strip():
            self.stderr.write("answer text must not be empty")
            raise SystemExit(2)
        try:
            with transaction.atomic():
                row = DeferredQuestion.consume(question_id, answer=text)
                if row is None:
                    self.stderr.write(f"question #{question_id} not found or already resolved")
                    raise SystemExit(1)
                row.resolved_via = DeferredQuestion.ResolvedVia.LOCAL
                row.save(update_fields=["resolved_via"])
                DeferredQuestionAudit.objects.create(
                    question=row,
                    action="answered",
                    answer_text=text,
                    resolver_id=resolver_id,
                )
                if row.parked_task is not None:
                    schedule_headless_resume(row.parked_task, answer=text)
        except DeferredQuestionError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(2) from exc
        return f"answered #{row.pk}."

    @command()
    def dismiss(
        self,
        question_id: int,
        reason: Annotated[
            str,
            typer.Option("--reason", help="Why the question is being dropped (audit trail)."),
        ] = "no longer relevant",
        resolver_id: Annotated[
            str,
            typer.Option("--resolver", help="Identity of the resolver (audit trail)."),
        ] = "",
    ) -> str:
        """Dismiss a pending question without answering it."""
        clean_reason = reason.strip() or "no longer relevant"
        try:
            with transaction.atomic():
                row = DeferredQuestion.consume(question_id, dismissed_reason=clean_reason)
                if row is None:
                    self.stderr.write(f"question #{question_id} not found or already resolved")
                    raise SystemExit(1)
                row.resolved_via = DeferredQuestion.ResolvedVia.LOCAL
                row.save(update_fields=["resolved_via"])
                DeferredQuestionAudit.objects.create(
                    question=row,
                    action="dismissed",
                    dismissed_reason=clean_reason,
                    resolver_id=resolver_id,
                )
        except DeferredQuestionError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(2) from exc
        return f"dismissed #{row.pk}."

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
        return f"resurfaced {delivered}/{total} pending question(s)."
