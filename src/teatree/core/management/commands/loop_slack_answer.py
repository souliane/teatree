"""``manage.py loop_slack_answer`` — one reactive Slack-answer cycle (#1014).

Structural clone of ``loop_self_improve``: acquires a dedicated
``LoopLease`` (``loop-slack-answer``) so a long answer cycle never blocks
a fast regular tick or a self-improve cycle, refuses to run when this
session is not the loop owner, runs :func:`run_slack_answer_cycle`, and
prints a one-line summary (or the JSON report when ``--json`` is passed).

This is a reactive ``/loop`` slot: a tight-cadence complement to the slower
per-loop ticks — a quick ack / status question gets a reply in seconds at
near-zero token cost, instead of waiting a full loop cadence.
"""

import datetime as dt
import json
import os
from dataclasses import asdict
from typing import Annotated

import typer
from django_typer.management import TyperCommand


def _non_owner_session_id() -> str | None:
    """Read the current Claude session id from the env, ``None`` when absent."""
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("T3_LOOP_SESSION_ID")


def _session_owns_loop(session_id: str | None) -> bool:
    """t3-master gate; ``None`` session ⇒ assume owner (CLI/manual use).

    Reads the same ``loop-registry.json`` ``_OWNER_LOOP`` record the
    hook_router writes at SessionStart — identical shape to
    ``loop_self_improve._session_owns_loop`` (the third slot must obey the
    same single-owner gate as the other two).
    """
    if not session_id:
        return True
    import json as _json  # noqa: PLC0415 — deferred: loaded only when this command runs
    from pathlib import Path  # noqa: PLC0415 — deferred: loaded only when this command runs

    base_env = os.environ.get("T3_LOOP_REGISTRY_DIR")
    base = Path(base_env) if base_env else Path.home() / ".local" / "share" / "teatree"
    registry_path = base / "loop-registry.json"
    if not registry_path.is_file():
        return True
    try:
        data = _json.loads(registry_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return True
    owner = data.get("t3-loop-tick-owner") if isinstance(data, dict) else None
    if not isinstance(owner, dict):
        return True
    return owner.get("session_id") == session_id


class Command(TyperCommand):
    help = "Run one reactive Slack-answer cycle (the third /loop slot)."

    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the cycle report as JSON."),
        ] = False,
    ) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.loop.slack_answer.cycle import run_slack_answer_cycle  # noqa: PLC0415 — lazy command import

        session_id = _non_owner_session_id()
        if not _session_owns_loop(session_id):
            now = dt.datetime.now(tz=dt.UTC)
            if json_output:
                self.stdout.write(
                    json.dumps(
                        {
                            "skipped": True,
                            "skipped_reason": "non-owner session",
                            "started_at": now.isoformat(),
                        },
                        indent=2,
                    )
                )
            else:
                self.stdout.write("SKIP  this session is not the loop owner — skipping Slack-answer cycle.")
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-slack-answer", owner=owner):
            now = dt.datetime.now(tz=dt.UTC)
            if json_output:
                self.stdout.write(
                    json.dumps(
                        {
                            "skipped": True,
                            "skipped_reason": "another Slack-answer cycle is already running",
                            "started_at": now.isoformat(),
                        },
                        indent=2,
                    )
                )
            else:
                self.stdout.write("SKIP  loop-slack-answer lease held — another cycle is running.")
            return
        try:
            report = run_slack_answer_cycle()
        finally:
            LoopLease.objects.release("loop-slack-answer", owner=owner)

        if json_output:
            self.stdout.write(json.dumps(asdict(report), indent=2, default=str))
            return
        self.stdout.write(
            f"OK    processed={report.processed} acked={report.acked} "
            f"simple={report.answered_simple} delegated={report.delegated} errors={report.errors}"
        )
