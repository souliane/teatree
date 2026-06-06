"""``t3 loop`` — start, stop, status, and one-shot tick of the fat loop.

The loop runs as a Claude Code ``/loop`` slot; this CLI manages the
slot's lifecycle and exposes ``tick`` for out-of-band invocations
(tests, manual debugging). ``start`` spawns a Claude Code session
with the loop pre-registered; ``stop`` prints the slot id to unregister
from inside the session.

Durability model (by design; #786 WS3 retired the immortal roster): the
loop is SESSION-BOUND and TICK-DRIVEN. It runs only while at least one
Claude Code session is open — spawning the per-unit sub-agent requires
the Agent tool, which exists only inside a live session. There is no
fixed roster of long-lived loop sub-agents and nothing to re-spawn from
a brief: the recurring ``t3 loop tick`` cron is the driver. Each tick the
single tick-owner session atomically claims the next pending DB unit
(``t3 loop claim-next``) and spawns ONE fresh, bounded sub-agent for just
that unit, which returns. Statelessness across ticks is the
compaction-proofing — a worker dying mid-task leaves its Task reclaimable
and the next tick re-dispatches it. Ownership is one Django-free record
(``_OWNER_LOOP``) naming which session is the tick-owner; if that session
dies, the next open session prunes it, becomes tick-owner, and keeps
ticking (it does NOT re-spawn anything). With ZERO sessions open the loop
is DEAD until the next session starts — accepted, not a defect;
the optional ``t3 loop install-watchdog`` (#1139) installs a macOS
LaunchAgent that bridges that gap by re-running ``spawn-headless`` on
session exit and after ``/login`` account switches.

The ``tick`` subcommand delegates to the ``loop_tick`` Django management
command via subprocess — anything that touches the Django ORM must be a
management command, not a plain typer command with manual ``django.setup()``.
"""

import os
import shutil
import sys
from pathlib import Path

import typer

from teatree.cli.loop_claim_next import claim_next_command
from teatree.cli.loop_list import list_command
from teatree.cli.loop_owner import register as register_loop_owner
from teatree.cli.loop_slack_answer import slack_answer_app
from teatree.cli.loop_watchdog import register as register_watchdog
from teatree.config import cadence_seconds
from teatree.loop.statusline import default_path
from teatree.utils.django_bootstrap import ensure_django

loop_app = typer.Typer(
    name="loop",
    help=(
        "Manage the tick-driven fat loop. Session-bound by design: it runs only "
        "while a Claude Code session is open. The recurring `t3 loop tick` cron is "
        "the driver — each tick the single tick-owner session atomically claims "
        "the next pending unit (`t3 loop claim-next`) and spawns one fresh bounded "
        "sub-agent for it. There is no roster of long-lived loop sub-agents to "
        "re-spawn (#786 WS3): if the owner session dies, the next open session "
        "becomes tick-owner and keeps ticking; with zero sessions open the loop is "
        "paused until the next session start (no OS daemon — accepted, not a "
        "defect). A per-agent Stop-hook self-pump re-continues the loop "
        "automatically while consolidated work remains — exactly one "
        "consolidation loop per agent identity, deduped across all sessions "
        "(#786 WS4); it idles when none."
    ),
    no_args_is_help=True,
)


@loop_app.command("tick")
def tick_command(
    *,
    statusline_file: Path = typer.Option(
        None,
        "--statusline-file",
        help="Override the statusline output path (test hook).",
    ),
    overlay: str = typer.Option(
        "",
        "--overlay",
        help="Restrict scanning to the named overlay (default: scan every registered overlay).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the tick report as JSON."),
) -> None:
    """Run one tick: scan in parallel, dispatch, render statusline.

    Delegates to the ``loop_tick`` Django management command so that
    Django is bootstrapped by the management framework (not manual
    ``django.setup()``).  All heavy imports (ORM, backends, scanners)
    live in the management command module, not here.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool | None] = {}
    if statusline_file is not None:
        kwargs["statusline_file"] = str(statusline_file)
    if overlay:
        kwargs["overlay"] = overlay
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_tick", **kwargs)


@loop_app.command("status")
def status_command() -> None:
    """Show the loop's last-rendered statusline."""
    target = default_path()
    if not target.is_file():
        typer.echo("No statusline rendered yet — run `t3 loop tick` first.")
        raise typer.Exit(code=1)
    typer.echo(target.read_text(encoding="utf-8"))


@loop_app.command("pending-spawn")
def pending_spawn_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit pending list as JSON."),
) -> None:
    """List pending Tasks (read-only probe; legacy — prefer ``claim-next``).

    Reads the dispatch DB (``Task`` rows in PENDING status) and prints
    each with its ``subagent`` hint. This is a pure read with NO claim:
    the spawn-then-``spawn-claim`` flow it used to drive was the
    double-dispatch race #786 WS1 replaced with the atomic
    ``t3 loop claim-next`` (claim-then-spawn). Retained for compatibility
    and as a non-mutating "is there pending work?" probe (e.g. the
    Stop-hook self-pump); the ``/loop`` slot should drive dispatch with
    ``claim-next``, not this + ``spawn-claim``.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_dispatch", "pending-spawn", **kwargs)


@loop_app.command("spawn-claim")
def spawn_claim_command(
    task_id: int = typer.Argument(..., help="Task PK to mark claimed."),
    *,
    claimed_by: str = typer.Option("loop-slot", "--claimed-by"),
) -> None:
    """Claim a Task by id (legacy — prefer atomic ``claim-next``).

    The retired spawn-then-claim flow called this AFTER dispatching
    ``Agent(...)``, leaving a window where two concurrent ticks both
    dispatched the same Task. #786 WS1's ``t3 loop claim-next`` claims
    atomically BEFORE the spawn (claim == spawn boundary) and is what the
    ``/loop`` slot should use. Retained for compatibility / explicit
    by-id claims; ``complete`` still happens when the sub-agent reports
    back via the standard TaskAttempt flow.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    call_command("loop_dispatch", "spawn-claim", str(task_id), claimed_by=claimed_by)


def _cadence_for_loop_slot() -> str:
    """Return the ``/loop <duration>`` argument.

    The cadence is resolved by :func:`cadence_seconds` — ``T3_LOOP_CADENCE``
    env first, then ``~/.teatree.toml`` ``loop_cadence_seconds`` (per-overlay
    override, then global ``[teatree]``), then the 720 default.
    """
    seconds = cadence_seconds()
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _stdin_is_terminal() -> bool:
    """Return whether stdin is a TTY — wrapped so tests can patch around ``runner.invoke``'s stdin replacement."""
    return sys.stdin.isatty()


@loop_app.command("start")
def start_command(
    *,
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the /loop slot definition instead of spawning a Claude Code session.",
    ),
) -> None:
    """Spawn a Claude Code session with the fat loop pre-registered.

    Looks for ``claude`` on ``PATH`` and runs it with an initial
    ``/loop <cadence> !t3 loop tick`` prompt so the loop is registered
    before the user types anything. When ``claude`` is not available or
    the caller is already inside a Claude Code session, falls back to
    printing the slash command for manual entry.

    Durability (by design; #786 WS3): the loop is session-bound and
    tick-driven. The SessionStart hook records ONE Django-free tick-owner
    record (``_OWNER_LOOP``: session_id/agent_id/pid/heartbeat — no
    per-loop briefs) in the machine-wide loop registry. There is no
    roster to re-spawn: the ``t3 loop tick`` cron drives the loop, each
    tick atomically claiming the next pending unit (``t3 loop
    claim-next``) and spawning one fresh bounded sub-agent for it. If
    this session dies, the next open session prunes the dead owner,
    becomes tick-owner, and keeps ticking. With no session open the loop
    is paused until the next session start. The optional ``install-watchdog``
    (#1139) installs a macOS LaunchAgent that re-runs ``spawn-headless`` so
    a fresh session is started after a crash or after ``/login`` account
    switches; absent that, the loop remains paused until the user reopens
    Claude Code.
    """
    cadence = _cadence_for_loop_slot()
    register_command = (
        f"/loop {cadence} Run `t3 loop tick`, then repeatedly run `t3 loop claim-next"
        " --json` until it returns nothing. Each call atomically claims ONE pending"
        " unit (#786 WS1 — no separate post-spawn claim step, no double-dispatch);"
        " for the returned entry call the Agent tool with subagent_type=entry.subagent,"
        " model=entry.model (omit to inherit the default tier), description=entry.execution_reason,"
        " and a prompt that includes entry.issue_url and instructs the sub-agent to load the"
        " skills in entry.skill_bundle first. When the sub-agent returns, record its JSON result"
        " envelope back with `t3 <overlay> tasks record-attempt entry.task_id '<result-json>'`"
        " so the task completes and the ticket advances. Subscription-covered: never `claude -p`."
    )

    if print_only or os.environ.get("CLAUDECODE") or not _stdin_is_terminal():
        typer.echo("Run this in your interactive Claude Code session to register the loop:")
        typer.echo(f"    {register_command}")
        typer.echo("")
        typer.echo(
            "Override the cadence with `T3_LOOP_CADENCE=<seconds> t3 loop start`, or set"
            " `loop_cadence_seconds` in `~/.teatree.toml` (env wins; default 720)."
        )
        typer.echo("")
        typer.echo(
            "The tick scans, dispatches, persists agent dispatches as Ticket+Task DB"
            " rows, and renders the statusline (display only). The slot atomically"
            " claims each pending Task via `t3 loop claim-next` and spawns one fresh"
            " bounded sub-agent in-session via its Agent tool. No detached `claude -p`,"
            " no queue files."
        )
        return

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("`claude` not found on PATH. Install Claude Code, then run:")
        typer.echo(f"    {register_command}")
        raise typer.Exit(code=1)

    typer.echo(f"Starting Claude Code with `{register_command}` …")
    os.execv(claude_bin, [claude_bin, register_command])  # noqa: S606  # Path comes from shutil.which; no shell, no user-controlled input.


@loop_app.command("stop")
def stop_command() -> None:
    """Print the slot id to stop in the Claude Code session."""
    typer.echo("To stop the loop, run `/loop unregister t3-loop` in the Claude Code session.")


# ── self-improve subcommands (BLUEPRINT § 5.7) ───────────────────────

self_improve_app = typer.Typer(
    name="self-improve",
    help=(
        "Self-improving monitor — scheduled smell detection with a tiered "
        "action ladder. Runs in the same loop-owner session as `t3 loop tick` "
        "on a separate LoopLease so a long self-improve cycle never blocks a "
        "fast regular tick (BLUEPRINT § 5.7)."
    ),
    no_args_is_help=True,
)


@self_improve_app.command("run")
def self_improve_run_command(
    *,
    tier: str = typer.Option(
        "cheap",
        "--tier",
        help="Cost tier: cheap|medium|expensive|all (default: cheap; Phase 1 ships cheap only).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the cycle report as JSON."),
) -> None:
    """Run one self-improve schedule cycle for the given tier."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool] = {"tier": tier}
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_self_improve", **kwargs)


@self_improve_app.command("status")
def self_improve_status_command(
    *,
    limit: int = typer.Option(20, "--limit", help="Max firings to show (default 20)."),
) -> None:
    """List the most recent SelfImproveFiring rows."""
    ensure_django()

    from teatree.core.models import SelfImproveFiring  # noqa: PLC0415

    rows = list(SelfImproveFiring.objects.all()[:limit])
    if not rows:
        typer.echo("No self-improve firings recorded.")
        return
    for row in rows:
        typer.echo(
            f"  [{row.severity}] {row.detector} -> {row.last_action} "
            f"(x{row.action_count}) {row.last_fired_at.isoformat()} :: {row.dedup_key}"
        )


def _self_improve_cadence_for_loop_slot() -> str:
    """Read ``T3_SELF_IMPROVE_CHEAP_CADENCE`` (seconds, default 1800)."""
    raw = os.environ.get("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800").strip() or "1800"
    try:
        seconds = max(60, int(raw))
    except ValueError:
        seconds = 1800
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@self_improve_app.command("start")
def self_improve_start_command() -> None:
    """Print the ``/loop <cadence>`` slot definition for the self-improve monitor.

    Mirrors ``t3 loop start --print-only``: it prints the slash command
    the user pastes inside the loop-owner Claude Code session to register
    the second ``/loop`` slot.  The cheap tier runs by default; override
    via ``T3_SELF_IMPROVE_CHEAP_CADENCE`` (seconds).
    """
    cadence = _self_improve_cadence_for_loop_slot()
    register_command = f"/loop {cadence} Run `t3 loop self-improve run --tier cheap`."
    typer.echo("Run this in your interactive Claude Code session to register the self-improve loop:")
    typer.echo(f"    {register_command}")
    typer.echo("")
    typer.echo(
        "Override the cadence with `T3_SELF_IMPROVE_CHEAP_CADENCE=<seconds> t3 loop self-improve start` "
        "(default 1800 = 30 min)."
    )
    typer.echo("")
    typer.echo(
        "The cycle scans Phase 1 detectors (dispatch_gap, forgotten_merge, "
        "stale_statusline_entry), dedups against the SelfImproveFiring DB, and "
        "advances the action ladder one rung per cool-down window (BLUEPRINT § 5.7)."
    )


loop_app.add_typer(self_improve_app, name="self-improve")

# #1073 — the session-scoped loop-owner hand-off CLI lives in its own
# module (split by concern; keeps this file under the module-health
# function budget). It registers flat `t3 loop claim/owner/release`.
register_loop_owner(loop_app)

# The reactive Slack-answer subapp (#1014, the third /loop slot) is
# assembled in ``teatree.cli.loop_slack_answer`` (imported at module top)
# so this file stays under the module-health public-function cap.
loop_app.add_typer(slack_answer_app, name="slack-answer")

# #1107 Prong C — the canonical atomic-claim CLI command lives in
# ``teatree.cli.loop_claim_next`` (split for the same module-health
# reason). Registered as a flat ``t3 loop claim-next``.
loop_app.command("claim-next")(claim_next_command)

# #1139 — laptop always-on session: `spawn-headless`, `install-watchdog`,
# `uninstall-watchdog`. Split off so this file stays under the module-health
# public-function cap.
register_watchdog(loop_app)

# #1744 — the read-only live loop-status view. Split off (same module-health
# reason) and registered as a flat ``t3 loop list``.
loop_app.command("list")(list_command)
