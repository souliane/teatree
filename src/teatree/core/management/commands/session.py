"""``t3 <overlay> session`` — session-lifecycle operations.

``todo-*`` is the durable per-session working list (souliane/teatree#3572). It
is harness-agnostic by construction: rows hang off ``Session``, whose identity
is a plain ``agent_id`` string resolved through
:func:`~teatree.core.session_identity.current_session_id`, so a background or
headless session — which has no harness TODO tool at all — keeps its in-flight
threads across turns and compaction instead of rendering them as chat text.
Distinct from the factory ``Task`` queue, which is dispatched headless *work*.

``prepare-stop`` refreshes the durable recovery artifacts on demand
(souliane/teatree#2564, PR-20): the TODO mirror, the resume-plan file, and
at-risk-worktree recovery. Idempotent and fast — the same
:func:`teatree.core.stop_snapshot.prepare_stop` the always-on 5-minute Stop
slot and the PreCompact compaction event call, so a manual run before stopping
and the background slot produce identical state.

ORM access is here (a management command, not a plain typer command) per the
project's "anything touching the ORM is a management command" rule.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from django.core.management.base import CommandError
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import Session, SessionTodo
from teatree.core.session_identity import current_session_id
from teatree.core.stop_snapshot import prepare_stop


class Command(TyperCommand):
    help = "Session-lifecycle operations."

    @initialize()
    def init(self) -> None:
        """``t3 <overlay> session`` group root."""

    @command(name="prepare-stop")
    def prepare_stop_cmd(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Refresh the durable recovery artifacts (idempotent, safe to re-run).

        Reports the resume-plan path, the TODO-mirror path, and any at-risk
        worktrees whose working state was captured for recovery. Re-running
        overwrites the files and the resume ref in place — no duplicate commits.
        """
        result = prepare_stop(current_session_id(), str(Path.cwd()))
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "todos_path": str(result.todos_path) if result.todos_path else None,
                        "resume_plan_path": str(result.resume_plan_path) if result.resume_plan_path else None,
                        "at_risk": [
                            {"path": str(w.path), "branch": w.branch, "recovery_ref": w.recovery_ref}
                            for w in result.at_risk
                        ],
                    },
                    indent=2,
                )
            )
            return
        self.stdout.write(f"OK    resume plan: {result.resume_plan_path}")
        self.stdout.write(f"      TODO mirror: {result.todos_path}")
        if result.at_risk:
            for wt in result.at_risk:
                self.stdout.write(f"      at-risk worktree captured: {wt.path} → {wt.recovery_ref}")
        else:
            self.stdout.write("      at-risk worktrees: none")

    def _resolve_session(self, session_pk: int | None) -> Session:
        """The session the TODO verbs act on: explicit ``--session``, else the live one.

        No auto-creation: a ``Session`` is ticket-anchored, so minting one here
        would invent a ticket association the caller never asked for. An
        unresolvable id fails with the id it looked for.
        """
        if session_pk is not None:
            return Session.objects.get(pk=session_pk)
        agent_id = current_session_id()
        if not agent_id:
            msg = "No live session id resolved (CLAUDE_CODE_SESSION_ID / T3_LOOP_SESSION_ID) — pass --session <pk>."
            raise CommandError(msg)
        session = Session.objects.filter(agent_id=agent_id).order_by("-pk").first()
        if session is None:
            msg = f"No session recorded for agent_id {agent_id!r} — pass --session <pk>."
            raise CommandError(msg)
        return session

    @command(name="todo-add")
    def todo_add(
        self,
        text: Annotated[str, typer.Argument(help="The working-list item.")],
        *,
        session_pk: Annotated[int | None, typer.Option("--session", help="Session pk (default: the live one).")] = None,
    ) -> None:
        """Append an item to this session's durable working list."""
        todo = SessionTodo.objects.add(self._resolve_session(session_pk), text)
        self.stdout.write(f"OK    added TODO {todo.pk}: {todo.text}")

    @command(name="todo-list")
    def todo_list(
        self,
        *,
        session_pk: Annotated[int | None, typer.Option("--session", help="Session pk (default: the live one).")] = None,
        all_items: Annotated[bool, typer.Option("--all", help="Include done items.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """List this session's working items, in working order."""
        session = self._resolve_session(session_pk)
        rows = SessionTodo.objects.filter(session=session) if all_items else SessionTodo.objects.open_for(session)
        if json_output:
            self.stdout.write(
                json.dumps([{"id": r.pk, "text": r.text, "status": r.status, "order": r.order} for r in rows], indent=2)
            )
            return
        if not rows:
            self.stdout.write("      (no items)")
            return
        for row in rows:
            self.stdout.write(f"  {row.pk:>4}  [{row.status}] {row.text}")

    @command(name="todo-set")
    def todo_set(
        self,
        todo_pk: Annotated[int, typer.Argument(help="TODO id.")],
        status: Annotated[str, typer.Argument(help="pending | in_progress | done")],
    ) -> None:
        """Move one working item to *status*."""
        if status not in SessionTodo.Status.values:
            msg = f"Unknown status {status!r} — one of {', '.join(SessionTodo.Status.values)}."
            raise CommandError(msg)
        todo = SessionTodo.objects.get(pk=todo_pk)
        todo.set_status(status)
        self.stdout.write(f"OK    TODO {todo.pk} -> {todo.status}")
