"""``t3 <overlay> honesty escalate`` — record a situational honesty escalation (#2263).

The agent-facing write seam for the honesty-critical escalation rule
(``skills/rules/SKILL.md`` §43). When the agent judges any of the four triggers
present — the user asked it to be honest, it judges it was dishonest, the user
accused it of lying, or it shipped a job it cannot verify is complete — it runs::

    t3 <overlay> honesty escalate --reason <user_asked|self_assessed_dishonest|accused_of_lying|shipped_incomplete>

before the next verification/review/grading spawn, so that work routes to the
most-honest configured model (``[agent] honesty_model``, default Opus). The
escalation is session-scoped (defaulting to the active
:func:`teatree.core.session_identity.current_session_id`), situational, and
auto-clears — it is not a standing reviewer-model change. The row is idempotent
on ``(session_id, task_id, reason)`` so re-firing the same trigger is a no-op.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models.honesty_escalation import HonestyEscalation
from teatree.core.session_identity import current_session_id

_REASONS = tuple(HonestyEscalation.Reason.values)


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> honesty`` group root."""

    @command()
    def escalate(
        self,
        *,
        reason: Annotated[
            str,
            typer.Option(
                "--reason",
                help="user_asked | self_assessed_dishonest | accused_of_lying | shipped_incomplete",
            ),
        ] = "",
        task: Annotated[
            int | None,
            typer.Option("--task", help="Optional task id to scope the escalation to."),
        ] = None,
        session: Annotated[
            str,
            typer.Option("--session", help="Session id (defaults to the active session)."),
        ] = "",
    ) -> str:
        """Record a honesty escalation so the next verification spawn routes to the most-honest model.

        The next ``(reviewing|requesting_review|testing)`` spawn for this session
        resolves to ``[agent] honesty_model`` (default Opus). Situational and
        auto-clearing — not a standing reviewer change.
        """
        if reason not in _REASONS:
            valid = " | ".join(_REASONS)
            self.stderr.write(f"  refused: --reason must be one of: {valid}")
            raise SystemExit(2)
        session_id = session.strip() or current_session_id()
        if not session_id:
            self.stderr.write("  refused: no session id (pass --session or run inside a session)")
            raise SystemExit(1)
        row = HonestyEscalation.record(reason, session_id=session_id, task_id=task)
        if row is None:
            return f"already escalated ({reason}) for session {session_id}."
        return f"escalated #{row.pk} ({reason}) for session {session_id} — next verification spawn → most-honest model."
