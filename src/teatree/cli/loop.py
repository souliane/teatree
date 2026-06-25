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
is DEAD until the next session starts — accepted, not a defect.

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
from teatree.cli.loop_claude_spec import register as register_claude_spec
from teatree.cli.loop_list import list_command
from teatree.cli.loop_owner import register as register_loop_owner
from teatree.cli.loop_slack_answer import slack_answer_app
from teatree.cli.loop_state import register as register_loop_state
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
    from teatree.loop.statusline_staleness import staleness_banner_for  # noqa: PLC0415

    target = default_path()
    if not target.is_file():
        typer.echo("No statusline rendered yet — run `t3 loop tick` first.")
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

    from django.core.management import call_command  # noqa: PLC0415

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

    from django.core.management import call_command  # noqa: PLC0415

    call_command("loop_dispatch", "spawn-claim", str(task_id), claimed_by=claimed_by)


def _format_loop_duration(seconds: int) -> str:
    """Format a cadence in seconds as a ``/loop <duration>`` argument."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _cadence_for_loop_slot() -> str:
    """Return the ``/loop <duration>`` argument.

    The cadence is resolved by :func:`cadence_seconds` — ``T3_LOOP_CADENCE``
    env first, then ``~/.teatree.toml`` ``loop_cadence_seconds`` (per-overlay
    override, then global ``[teatree]``), then the 720 default.
    """
    return _format_loop_duration(cadence_seconds())


def _dedicated_loops_enabled() -> bool:
    """Whether the dedicated-loop slot generator is enabled (#1838, default OFF).

    Resolved through :func:`teatree.config.resolution.get_effective_settings`,
    so the layers — ``T3_DEDICATED_LOOPS`` env, the per-overlay
    ``[overlays.<name>]`` override, the global ``[teatree]`` value, then the
    ``False`` default — are honoured. Fail OFF: any resolution failure leaves
    the single fat slot in place (byte-identical to today).
    """
    from teatree.config.resolution import get_effective_settings  # noqa: PLC0415

    return bool(get_effective_settings().dedicated_loops)


def _fat_loop_slot() -> str:
    """The single fat ``/loop <cadence> Run `t3 loop tick`…`` slot definition."""
    return (
        f"/loop {_cadence_for_loop_slot()} Run `t3 loop tick`, then repeatedly run `t3 loop claim-next"
        " --json` until it returns nothing. Each call atomically claims ONE pending"
        " unit (#786 WS1 — no separate post-spawn claim step, no double-dispatch);"
        " for the returned entry call the Agent tool with subagent_type=entry.subagent,"
        " model=entry.model (omit to inherit the default tier), description=entry.execution_reason,"
        " and a prompt that includes entry.issue_url and instructs the sub-agent to load the"
        " skills in entry.skill_bundle first. When entry.fanout_directive is non-empty, append"
        " it verbatim to that prompt (it directs the sub-agent to fan its work out — e.g."
        " adversarial-verify on review, judge-panel on planning); when it is empty, append"
        " nothing. When the sub-agent returns, record its JSON result"
        " envelope back with `t3 <overlay> tasks record-attempt entry.task_id '<result-json>'`"
        " so the task completes and the ticket advances. Subscription-covered: never `claude -p`."
    )


def _dedicated_loop_slots() -> list[str]:
    """One scoped-tick ``/loop`` slot per dedicated loop (#1838).

    Each dedicated loop (a named group of mini-loops) gets its own
    ``/loop <cadence> Run `t3 loop tick --slot <name>`…`` slot driving a
    SCOPED tick that claims ``loop:<name>`` and dispatches only that group's
    members — splitting the single fat loop into N independently-owned loops.
    """
    from teatree.loops.dedicated import iter_dedicated_loops  # noqa: PLC0415

    slots: list[str] = []
    for dl in iter_dedicated_loops():
        cadence = _format_loop_duration(dl.cadence_seconds)
        slots.append(
            f"/loop {cadence} Run `t3 loop tick --slot {dl.name}`, then repeatedly run `t3 loop"
            " claim-next --json` until it returns nothing. Each call atomically claims ONE pending"
            " unit; for the returned entry call the Agent tool with subagent_type=entry.subagent,"
            " model=entry.model (omit to inherit the default tier), description=entry.execution_reason,"
            " and a prompt that includes entry.issue_url and instructs the sub-agent to load the"
            " skills in entry.skill_bundle first. When entry.fanout_directive is non-empty, append it"
            " verbatim. When the sub-agent returns, record its JSON result envelope with"
            " `t3 <overlay> tasks record-attempt entry.task_id '<result-json>'`."
            " Subscription-covered: never `claude -p`."
        )
    return slots


def loop_slots() -> list[str]:
    """The ``/loop`` slot definition(s) to register for this deployment.

    Returns the single fat slot when ``dedicated_loops`` is OFF (default —
    byte-identical to today), or the N dedicated scoped-tick slots when ON.
    The two are mutually-exclusive deployment modes: in dedicated mode the
    fat slot is NOT emitted, and vice-versa.
    """
    if _dedicated_loops_enabled():
        return _dedicated_loop_slots()
    return [_fat_loop_slot()]


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
    print_slots: bool = typer.Option(
        False,
        "--print-slots",
        help=(
            "Print the resolved /loop slot definition(s) and exit, without spawning. "
            "One fat slot when `dedicated_loops` is off (default); N dedicated scoped-tick "
            "slots when on. Inspect the slot generator without registering anything."
        ),
    ),
) -> None:
    """Spawn a Claude Code session with the loop pre-registered.

    Looks for ``claude`` on ``PATH`` and runs it with an initial
    ``/loop <cadence> !t3 loop tick`` prompt so the loop is registered
    before the user types anything. When ``claude`` is not available or
    the caller is already inside a Claude Code session, falls back to
    printing the slash command for manual entry.

    Slot topology is governed by the default-off ``[loops] dedicated_loops``
    toggle (#1838). OFF (default): the single fat ``loop-owner`` slot drives
    one ``t3 loop tick`` fanning across ALL mini-loops — byte-identical to
    today. ON: ``--print-slots`` (or the manual-paste fallback) emits N
    dedicated ``/loop <cadence> Run `t3 loop tick --slot <name>``` slots, one
    per dedicated loop, each scoped to its own ``loop:<name>`` owner. The two
    are mutually-exclusive deployment modes.

    Durability (by design; #786 WS3): the loop is session-bound and
    tick-driven. The SessionStart hook records ONE Django-free tick-owner
    record (``_OWNER_LOOP``: session_id/agent_id/pid/heartbeat — no
    per-loop briefs) in the machine-wide loop registry. There is no
    roster to re-spawn: the ``t3 loop tick`` cron drives the loop, each
    tick atomically claiming the next pending unit (``t3 loop
    claim-next``) and spawning one fresh bounded sub-agent for it. If
    this session dies, the next open session prunes the dead owner,
    becomes tick-owner, and keeps ticking. With no session open the loop
    is paused until the next session start.
    """
    slots = loop_slots()

    if print_slots:
        typer.echo("Run these in your interactive Claude Code session to register the loop:")
        for slot in slots:
            typer.echo(f"    {slot}")
        return

    register_command = slots[0]
    dedicated = len(slots) > 1

    if print_only or os.environ.get("CLAUDECODE") or not _stdin_is_terminal():
        if dedicated:
            typer.echo("Dedicated-loop mode — run these in your interactive Claude Code session:")
            for slot in slots:
                typer.echo(f"    {slot}")
            typer.echo("")
            typer.echo(
                "Each scoped slot claims its own `loop:<name>` owner and dispatches only that"
                " dedicated loop's members. Toggle off with `dedicated_loops = false` in"
                " `~/.teatree.toml` (or `T3_DEDICATED_LOOPS=0`) for the single fat slot."
            )
            return
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

    argv = [claude_bin, *_session_pin_flags(), register_command]
    typer.echo(f"Starting Claude Code with `{register_command}` …")
    os.execv(claude_bin, argv)  # noqa: S606  # Path comes from shutil.which; no shell, no user-controlled input.


def _session_pin_flags() -> list[str]:
    """The interactive main-agent ``--model`` / ``--effort`` pins from ``[agent]``.

    These are session-level pins so the user never runs ``/model`` (or sets the
    effort) by hand. They are injected ONLY into the interactive ``claude``
    spawn argv here — never into ``claude -p`` headless (effort is session-wide,
    not per-sub-agent). Absent settings inject nothing, so the spawn is
    byte-for-byte today's behaviour. Effort is validated at parse time
    (``config_agent.resolve_agent_config``), so an off-scale value fails loudly
    rather than reaching the CLI.

    The ``session_model`` pin passes through the same Fable kill-switch
    (``_downgrade_fable``) the headless spawn chokepoint uses (teatree#2237), so
    a Fable session pin downgrades to the Opus 4.8 baseline when
    ``[agent] fable_enabled = false`` — the one flip reverts every surface.
    """
    from teatree.agents.model_tiering import _downgrade_fable  # noqa: PLC0415
    from teatree.config_agent import resolve_agent_config  # noqa: PLC0415

    cfg = resolve_agent_config()
    session_model = _downgrade_fable(cfg.session_model, cfg)
    flags: list[str] = []
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

# #1744 — the read-only live loop-status view. Split off (same module-health
# reason) and registered as a flat ``t3 loop list``.
loop_app.command("list")(list_command)

# #1913 — the DB-backed per-loop control plane: flat ``t3 loop
# pause/resume/disable/enable <name>`` + ``t3 loop loop-state <name>``. Split
# off (same module-health reason) into ``teatree.cli.loop_state``.
register_loop_state(loop_app)

# #2650 — flat ``t3 loop claude-spec <name>``: print a loop's native Claude
# ``/loop`` spec (slot_id + cron + prompt) the ``/t3:loops`` enable/disable skill
# mirrors via CronCreate/CronDelete. Split off (same module-health reason).
register_claude_spec(loop_app)
