"""``manage.py loop_slack_answer`` — one reactive Slack-answer cycle (#1014).

Structural clone of ``loop_self_improve``: acquires a dedicated
``LoopLease`` (``loop-slack-answer``) so a long answer cycle never blocks
a fast regular tick or a self-improve cycle, refuses to run when this
session is not the loop owner, runs :func:`run_slack_answer_cycle`, and
prints a one-line summary (or the JSON report when ``--json`` is passed).

This is a reactive ``/loop`` slot complementing the slower per-loop ticks —
a quick ack / status question gets a reply in seconds at near-zero token cost.
The inbound-event wake is the primary drain (~1s); this timer slot is the
fallback safety net on a 5m default cadence.
"""

import datetime as dt
import os
from dataclasses import asdict
from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand

from teatree.core.machine_output import emit
from teatree.core.session_identity import session_id_from_env
from teatree.utils.hook_registry import loop_registry_dir


def _non_owner_session_id() -> str | None:
    """Read the current Claude session id from the env, ``None`` when absent."""
    return session_id_from_env()


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

    registry_path = loop_registry_dir() / "loop-registry.json"
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

        out = cast("IO[str]", self.stdout)
        err = cast("IO[str]", self.stderr)
        session_id = _non_owner_session_id()
        if not _session_owns_loop(session_id):
            now = dt.datetime.now(tz=dt.UTC)
            emit(
                {"skipped": True, "skipped_reason": "non-owner session", "started_at": now.isoformat()},
                json_output=json_output,
                out=out,
                err=err,
                human="SKIP  this session is not the loop owner — skipping Slack-answer cycle.",
            )
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-slack-answer", owner=owner):
            now = dt.datetime.now(tz=dt.UTC)
            emit(
                {
                    "skipped": True,
                    "skipped_reason": "another Slack-answer cycle is already running",
                    "started_at": now.isoformat(),
                },
                json_output=json_output,
                out=out,
                err=err,
                human="SKIP  loop-slack-answer lease held — another cycle is running.",
            )
            return
        try:
            report = run_slack_answer_cycle()
        finally:
            LoopLease.objects.release("loop-slack-answer", owner=owner)

        emit(
            asdict(report),
            json_output=json_output,
            out=out,
            err=err,
            human=(
                f"OK    processed={report.processed} acked={report.acked} "
                f"simple={report.answered_simple} dispatched={report.dispatched} "
                f"covered={report.covered} errors={report.errors}"
            ),
        )
