"""``t3 teatree safe-kill <pid> --hang-cause "<why>"`` — the runnable safe-kill guard (#2225).

The runnable surface the PreToolUse raw-pid-kill deny points the agent at. It
routes a kill through :func:`teatree.core.safe_kill.safe_kill`, which signals the
process ONLY when both hold: the pid maps to a KNOWN dead/failed target by
session id, and two CPU samples confirm it is non-live. On a refusal the process
is NEVER signalled and the evidence (candidate session id + liveness STAT) is
printed so the agent confirms the target id with the user first.

Exit code 0 when the signal was sent (verified-dead target); exit code 1 when
the guard refused — so a script can branch on the outcome.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.safe_kill import safe_kill


class Command(TyperCommand):
    def handle(
        self,
        pid: Annotated[int, typer.Argument(help="The OS process id to signal.")],
        hang_cause: Annotated[
            str,
            typer.Option("--hang-cause", help="Why the target is believed hung (required — a bare guess is refused)."),
        ] = "",
    ) -> str:
        """Signal *pid* only if it maps to a dead target AND is confirmed non-live."""
        verdict = safe_kill(pid, hang_cause=hang_cause)
        if verdict.allowed:
            return f"safe-kill: signalled pid {pid} (session {verdict.identity.session_id})."
        raise SystemExit(verdict.reason)
