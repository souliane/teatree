"""``manage.py loop_self_improve`` — one schedule cycle of the self-improve monitor.

Mirrors the shape of ``loops_tick``: acquires a dedicated ``LoopLease``
(``loop-self-improve``) so a long self-improve cycle never blocks a fast
regular tick, refuses to run when this session is not the loop owner,
runs the tier dispatcher, and prints a one-line summary (or the JSON
report when ``--json`` is passed).
"""

import datetime as dt
import json
import os
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any

import typer
from django_typer.management import TyperCommand

if TYPE_CHECKING:
    from teatree.loop.self_improve.schedule import TierResult

type ReportDict = dict[str, Any]


def _result_to_dict(result: "TierResult") -> ReportDict:
    return {
        "tier": result.tier,
        "budget_ok": result.budget.ok,
        "budget_reason": result.budget.reason,
        "skipped": result.skipped,
        "report_count": len(result.reports),
        "action_count": len(result.actions),
        "reports": [asdict(r) for r in result.reports],
        "actions": [
            {
                "rung": a.rung,
                "firing_id": a.firing.pk,
                "detector": a.firing.detector,
                "dedup_key": a.firing.dedup_key,
                "slack_capped": a.slack_capped,
                "auto_fix_executed": a.auto_fix_executed,
            }
            for a in result.actions
        ],
    }


def _non_owner_session_id() -> str | None:
    """Read the current Claude session id from the env, ``None`` when absent.

    Mirrors ``hook_router._session_owns_loop``: when teatree is not
    running inside a session at all (e.g. a manual CLI invocation), the
    env var is missing and the t3-master gate skips its check rather
    than refusing every CLI call.
    """
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("T3_LOOP_SESSION_ID")


def _session_owns_loop(session_id: str | None) -> bool:
    """t3-master gate; ``None`` session ⇒ assume owner (CLI/manual use).

    Inside a Claude Code session the env var is set; outside (tests,
    direct CLI) it isn't and the gate is bypassed — same shape as the
    existing tick-owner record consultation.  Reads the same
    ``loop-registry.json`` ``_OWNER_LOOP`` record the hook_router writes
    at SessionStart — the only durable place that knows which session
    is currently the loop owner.
    """
    if not session_id:
        return True
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    base_env = os.environ.get("T3_LOOP_REGISTRY_DIR")
    base = Path(base_env) if base_env else Path.home() / ".local" / "share" / "teatree"
    registry_path = base / "loop-registry.json"
    if not registry_path.is_file():
        return True
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return True
    owner = data.get("t3-loop-tick-owner") if isinstance(data, dict) else None
    if not isinstance(owner, dict):
        return True
    return owner.get("session_id") == session_id


class Command(TyperCommand):
    help = "Run one schedule cycle of the self-improving monitor."

    def handle(
        self,
        *,
        tier: Annotated[
            str,
            typer.Option("--tier", help="Cost tier (cheap/medium/expensive/all). Default: cheap."),
        ] = "cheap",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the cycle report as JSON."),
        ] = False,
    ) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.phases.render import self_improve_rerender  # noqa: PLC0415
        from teatree.loop.self_improve.schedule import run_tier  # noqa: PLC0415

        session_id = _non_owner_session_id()
        if not _session_owns_loop(session_id):
            now = dt.datetime.now(tz=dt.UTC)
            if json_output:
                self.stdout.write(
                    json.dumps(
                        {
                            "tier": tier,
                            "skipped": True,
                            "skipped_reason": "non-owner session",
                            "started_at": now.isoformat(),
                        },
                        indent=2,
                    )
                )
            else:
                self.stdout.write("SKIP  this session is not the loop owner — skipping self-improve cycle.")
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-self-improve", owner=owner):
            now = dt.datetime.now(tz=dt.UTC)
            if json_output:
                self.stdout.write(
                    json.dumps(
                        {
                            "tier": tier,
                            "skipped": True,
                            "skipped_reason": "another self-improve cycle is already running",
                            "started_at": now.isoformat(),
                        },
                        indent=2,
                    )
                )
            else:
                self.stdout.write("SKIP  loop-self-improve lease held — another cycle is running.")
            return
        try:
            result = run_tier(tier, auto_fix_callable=self_improve_rerender)
        finally:
            LoopLease.objects.release("loop-self-improve", owner=owner)

        report = _result_to_dict(result)
        if json_output:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        if result.skipped:
            self.stdout.write(f"SKIP  budget gate: {result.budget.reason}")
            return
        self.stdout.write(f"OK    tier={result.tier} reports={len(result.reports)} actions={len(result.actions)}")
