"""``t3 loop`` — start, stop, status, and the reactive-loop lifecycle helpers.

The autonomous loops run as durable self-rescheduling loop-timer chains that the
singleton ``t3 worker`` drains (#1796 / PR-28: one ``loop_timer`` chain per enabled
DB ``Loop`` row, each firing ``t3 loops tick --loop <name>`` on its own cadence —
there is no master tick). This CLI manages that lifecycle: ``start`` spawns a Claude
Code session (whose owner hook registers the reactive infra ``/loop``s); ``stop``
prints the slot id to unregister; ``list`` / ``status`` read live loop state; and
the reactive infra loops (``self-improve``, ``slack-answer``, ``drain-queue``) each
expose their own ``run`` / ``start`` subcommands here.

Durability model: the worker owns the per-loop tick cadence by default, so the DB
loops run with NO Claude Code session open (the SessionStart supervisor keeps at
least one worker alive; ``loop_runner_enabled`` OFF stops them entirely — there is
no fallback plane, PR-28 retired the native ``/loop`` cron mirror). Each per-loop
tick atomically claims the next pending DB unit (``t3 loop claim-next``) and spawns
ONE fresh, bounded sub-agent for just that unit, which returns; spawning the
sub-agent requires the Agent tool, which exists only inside a live Claude session,
so the worker's deadlined tick subprocess dispatches work when a session is present.
Statelessness across ticks is the compaction-proofing — a worker dying mid-task
leaves its Task reclaimable and the next tick re-dispatches it. Ownership is
per-loop (the ``loop:<name>`` lease).
"""

import os
import shutil
import sys
from pathlib import Path

import typer

from teatree.agents import permission_modes
from teatree.cli.loop.claim_next import claim_next_command
from teatree.cli.loop.drain_queue import drain_queue_app
from teatree.cli.loop.intake_loops import intake_loops_command
from teatree.cli.loop.listing import list_command
from teatree.cli.loop.owner import register as register_loop_owner
from teatree.cli.loop.preset import register as register_loop_preset
from teatree.cli.loop.reclaim_markers import reclaim_markers_command
from teatree.cli.loop.schedule import register as register_loop_schedule
from teatree.cli.loop.slack_answer import slack_answer_app
from teatree.cli.loop.state import register as register_loop_state
from teatree.config import cadence_seconds
from teatree.loop.loop_cadences import reactive_slot
from teatree.loop.statusline import default_path
from teatree.utils.django_bootstrap import ensure_django

loop_app = typer.Typer(
    name="loop",
    help=(
        "Manage the tick-driven autonomous loops. Under #1796 / PR-28 the singleton "
        "`t3 worker` owns the per-loop tick cadence by default (`loop_runner_enabled` "
        "ON): it drains durable self-rescheduling loop-timer chains (django-tasks "
        "`run_after` rows), one per enabled DB `Loop` row firing "
        "`t3 loops tick --loop <name>` on its own cadence — there is no master tick, "
        "and the DB loops run with no Claude session open (the SessionStart supervisor "
        "keeps one worker alive; on a headless box start it once from a login "
        "profile). `loop_runner_enabled` is the kill-switch — set it false to stop the "
        "loops entirely (there is no fallback plane; PR-28 retired the native `/loop` "
        "cron mirror). Each per-loop tick atomically claims the next pending unit "
        "(`t3 loop claim-next`) and spawns one fresh bounded sub-agent for it; a "
        "dying worker leaves its Task reclaimable and the next tick re-dispatches it. "
        "Check the worker with `t3 worker status`; ensure one is running with "
        "`t3 worker ensure`."
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
    """Run one user-manual full-scan tick by hand: scan every overlay, dispatch, render.

    NOT the loop driver (#2650): the automated loop is per-loop
    (``t3 loops tick --loop <name>``). This is the by-hand diagnostic — it claims no
    owner lease and is not gated by the DB ``Loop`` table, so it scans the full
    default scanner set regardless of which loops are enabled. Delegates to the
    ``loop_tick`` management command; the system never uses it to drive itself
    (autonomous-lane redesign §7).
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

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
    from teatree.loop.statusline_staleness import staleness_banner_for  # noqa: PLC0415 — deferred: lazy CLI import

    target = default_path()
    if not target.is_file():
        typer.echo("No statusline rendered yet — run `t3 loops tick --loop <name>` first.")
        raise typer.Exit(code=1)
    # A frozen statusline (dead/stopped loop) is displayed verbatim — prepend a
    # RED staleness banner when the render age crosses the cutoff so the reader
    # is never misled by a confident, hours-old loop line. Fails open to "".
    banner = staleness_banner_for(target, cadence_seconds=cadence_seconds())
    if banner:
        typer.echo(banner)
    typer.echo(target.read_text(encoding="utf-8"))


@loop_app.command("pending-spawn")
def pending_spawn_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit pending list as JSON."),
    claimable_only: bool = typer.Option(
        False,
        "--claimable-only",
        help="Report work ONLY when a claim could land (honour the admit budget).",
    ),
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

    ``--claimable-only`` (TODO #100) makes the probe budget-aware: it
    reports work ONLY when a unit ``claim-next`` could actually claim,
    so the Stop-hook self-pump stops re-offering a PENDING unit that a
    full in-flight admit budget will always refuse.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    if claimable_only:
        kwargs["claimable_only"] = True
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

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("loop_dispatch", "spawn-claim", str(task_id), claimed_by=claimed_by)


def _stdin_is_terminal() -> bool:
    """Return whether stdin is a TTY — wrapped so tests can patch around ``runner.invoke``'s stdin replacement."""
    return sys.stdin.isatty()


_REGISTER_GUIDANCE = (
    "PR-28: the singleton `t3 worker` owns the per-loop tick cadence by default "
    "(`loop_runner_enabled` ON), draining the durable self-rescheduling loop-timer "
    "chains — so the DB loops run with no Claude session open. Check it with "
    "`t3 worker status`; ensure one is running with `t3 worker ensure`. Enable or "
    "disable an individual loop with `t3 loop enable|disable <name>` (the reconciler "
    "adds/prunes its timer at once). This session still registers the reactive infra "
    "loops (self-improve, slack-answer, drain-queue) automatically at start."
)


@loop_app.command("start")
def start_command(
    *,
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the per-loop registration guidance instead of spawning a Claude Code session.",
    ),
) -> None:
    """Spawn a Claude Code session; the t3-master registers each enabled loop's ``/loop``.

    Looks for ``claude`` on ``PATH`` and spawns it (with the interactive session
    model/effort pins). Under #2650 the live set of native Claude ``/loop``s
    mirrors the ENABLED ``Loop`` rows — ONE ``/loop`` per loop firing
    ``t3 loops tick --loop <name>`` — and the SessionStart t3-master hook
    registers them automatically, so there is no single fat slot to pass on the
    command line. When ``claude`` is unavailable or the caller is already inside a
    Claude Code session, prints the per-loop registration guidance instead.

    Durability (by design; #786 WS3): the loop is session-bound and tick-driven.
    With no session open the loop is paused until the next session start.
    """
    if print_only or os.environ.get("CLAUDECODE") or not _stdin_is_terminal():
        typer.echo("Start a Claude Code session; the t3-master registers each enabled loop's `/loop` automatically.")
        typer.echo("")
        typer.echo(_REGISTER_GUIDANCE)
        return

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("`claude` not found on PATH. Install Claude Code, then start a session — the t3-master")
        typer.echo("registers each enabled loop's `/loop` automatically.")
        typer.echo("")
        typer.echo(_REGISTER_GUIDANCE)
        raise typer.Exit(code=1)

    argv = [claude_bin, *_session_pin_flags()]
    typer.echo("Starting Claude Code — the t3-master session registers each enabled loop's `/loop`…")
    os.execv(claude_bin, argv)  # noqa: S606  # Path comes from shutil.which; no shell, no user-controlled input.


def _session_pin_flags() -> list[str]:
    """The loop session's ``--permission-mode`` / ``--model`` / ``--effort`` pins.

    The mode is pinned UNCONDITIONALLY. This session is attended at LAUNCH —
    ``start_command`` refuses a non-terminal stdin, and the operator is there to
    see the child's own auth behaviour, which is why the base-URL guard
    deliberately skips this exec. But it long outlives that moment: it drives the
    autonomous loop and keeps ticking unattended under ``autonomous_away``, with
    nobody present to override a classifier denial. So it must not inherit the
    operator's ``permissions.defaultMode`` — ``t3 doctor check`` advises ``auto``
    there, and pinning here is what makes that advice safe to follow. It is the
    same :data:`~teatree.agents.permission_modes.UNATTENDED` the headless dispatch
    options pin, so the unattended lanes cannot drift apart.

    Model and effort are session-level pins so the user never runs ``/model`` (or
    sets the effort) by hand. They are injected ONLY into the interactive
    ``claude`` spawn argv here — never into ``claude -p`` headless (effort is
    session-wide, not per-sub-agent). An absent ``agent_session_model`` injects no
    ``--model``; effort ships a default (``xhigh``), so ``--effort`` is always
    injected unless the operator pins a different scale value. Effort is validated
    at parse time (``config.agent_spawn.resolve_agent_config``), so an off-scale
    value fails loudly rather than reaching the CLI.
    """
    from teatree.config.agent_spawn import resolve_agent_config  # noqa: PLC0415 — deferred: interactive-spawn path only

    cfg = resolve_agent_config()
    session_model = cfg.session_model
    flags: list[str] = ["--permission-mode", permission_modes.UNATTENDED]
    if session_model:
        flags.extend(["--model", session_model])
    if cfg.session_effort:
        flags.extend(["--effort", cfg.session_effort])
    return flags


@loop_app.command("stop")
def stop_command() -> None:
    """Print the slot id to stop in the Claude Code session."""
    typer.echo("To stop the loop, run `/loop unregister t3-loop` in the Claude Code session.")


# ── self-improve subcommands (BLUEPRINT § 5.7) ───────────────────────

self_improve_app = typer.Typer(
    name="self-improve",
    help=(
        "Self-improving monitor — scheduled smell detection with a tiered "
        "action ladder. Runs as its own dedicated `/loop` slot on a separate "
        "`loop-self-improve` LoopLease so a long self-improve cycle never blocks "
        "a fast per-loop tick (BLUEPRINT § 5.7)."
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

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

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

    from teatree.core.models import SelfImproveFiring  # noqa: PLC0415 — deferred: ORM import needs the app registry

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
    """The ``loop-self-improve`` ``/loop`` cadence token — the reactive slot is the single source of truth."""
    return reactive_slot("loop-self-improve").cadence()


@self_improve_app.command("start")
def self_improve_start_command() -> None:
    """Print the ``/loop <cadence>`` slot definition for the self-improve monitor.

    Mirrors ``t3 loop start --print-only``: it prints the slash command
    the user pastes inside the t3-master Claude Code session to register
    the second ``/loop`` slot.  The cheap tier runs by default; override
    via ``T3_SELF_IMPROVE_CHEAP_CADENCE`` (seconds).
    """
    register_command = reactive_slot("loop-self-improve").loop_directive()
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

# #1073 — the session-scoped t3-master hand-off CLI lives in its own
# module (split by concern; keeps this file under the module-health
# function budget). It registers flat `t3 loop claim/owner/release`.
register_loop_owner(loop_app)

# The reactive Slack-answer subapp (#1014) is assembled in
# ``teatree.cli.loop.slack_answer`` (imported at module top) so this file stays
# under the module-health public-function cap.
loop_app.add_typer(slack_answer_app, name="slack-answer")

# The reactive DB-queue drain subapp is assembled in
# ``teatree.cli.loop.drain_queue`` (imported at module top), same module-health
# split; its own dedicated `/loop` replaces the retired won-tick piggyback drain.
loop_app.add_typer(drain_queue_app, name="drain-queue")

# #1107 Prong C — the canonical atomic-claim CLI command lives in
# ``teatree.cli.loop.claim_next`` (split for the same module-health
# reason). Registered as a flat ``t3 loop claim-next``.
loop_app.command("claim-next")(claim_next_command)

# #1744 — the read-only live loop-status view. Split off (same module-health
# reason) and registered as a flat ``t3 loop list``.
loop_app.command("list")(list_command)

# #3632 — the DB-free owner-intake loop names the deploy fleet policy must never
# force off; a flat ``t3 loop intake-loops`` the entrypoint reseed reads.
loop_app.command("intake-loops")(intake_loops_command)

# #3275 — the on-demand issue-marker reconciler: flat ``t3 loop reclaim-markers``.
# The sanctioned way to unjam stranded intake budget (raw SQL is classifier-blocked).
loop_app.command("reclaim-markers")(reclaim_markers_command)

# #1913 — the DB-backed per-loop control plane: flat ``t3 loop
# pause/resume/disable/enable <name>`` + ``t3 loop loop-state <name>``. Split
# off (same module-health reason) into ``teatree.cli.loop.state``.
register_loop_state(loop_app)

# #3159 — the preset + weekly-schedule layer: ``t3 loop preset …`` and
# ``t3 loop schedule …`` (each a nested subgroup delegating to a management
# command), split into ``teatree.cli.loop.preset`` / ``…schedule``.
register_loop_preset(loop_app)
register_loop_schedule(loop_app)
