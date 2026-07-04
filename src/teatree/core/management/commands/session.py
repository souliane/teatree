"""``t3 <overlay> session`` — session-lifecycle operations.

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
from django_typer.management import TyperCommand, command, initialize

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
