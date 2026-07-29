"""``t3 worker`` — the singleton loop-timer worker + its lifecycle controls (#1796).

The worker acquires the ``worker`` flock singleton and runs K pinned ``django_tasks_db``
executor threads that drain the self-rescheduling loop-timer chains (no OS cron /
launchd / systemd). PR-28 flipped ``loop_runner_enabled`` ON by default, so the worker
owns the tick cadence out of the box; the SessionStart supervisor + ``t3 worker ensure``
keep at least one alive. Bare ``t3 worker`` runs the worker (the ``run`` alias — one
documented invocation path); ``status``, ``ensure``, ``drain``, ``stop`` and ``restart``
are the operator controls.

``drain`` closes admission without stopping anything (the deploy verb — a fresh container
boot clears the gate). ``stop`` / ``restart`` are the bare-host verbs: they drain, signal
the flock holder, and PROVE the outcome against the kernel flock probe, leaving
``worker_quiescing`` in a state the operator is told about rather than one they must
discover.
"""

import json
from typing import TYPE_CHECKING, TypedDict

import typer

if TYPE_CHECKING:
    from teatree.loop.drain import DrainReport
    from teatree.loop.worker_lifecycle import StopReport


class DrainPayload(TypedDict):
    """The JSON shape ``stop --json`` emits for a completed drain."""

    outcome: str
    still_claimed: list[int]


worker_app = typer.Typer(
    name="worker",
    help=(
        "The singleton loop-timer worker (#1796 / PR-28). Bare `t3 worker` runs it "
        "(the cadence owner, default ON via `loop_runner_enabled`). `status` reports "
        "the live holder + resolved kill-switch + whether loops actually tick (it EXITS "
        "NON-ZERO on a stale fleet); `ensure` spawns a detached worker iff enabled and "
        "the flock is free; `drain` quiesces admission without stopping anything; "
        "`stop` / `restart` end the live worker and verify it against the flock."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)

#: Printed whenever the admission gate is left ON — the recovery must be discoverable from
#: the command that closed it, never only from the source. `t3 worker restart` clears the
#: gate as it brings the fresh worker up; on a bare host nothing else does.
_QUIESCED_NOTICE = (
    "NOTE worker_quiescing is ON — the claim path admits ZERO new work, and on a bare host "
    "nothing clears it (only a container deploy's fresh init does). Clear it with "
    "`t3 <overlay> config_setting set worker_quiescing false`, or run `t3 worker restart` "
    "(it clears the gate as it starts the fresh worker)."
)


def _run_worker() -> None:
    """Run the singleton loop-timer worker in the foreground (the cadence owner)."""
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    call_command("worker")


@worker_app.callback(invoke_without_command=True)
def _worker_root(ctx: typer.Context) -> None:
    """Bare ``t3 worker`` runs the worker; a subcommand (`status`/`ensure`) takes over."""
    if ctx.invoked_subcommand is None:
        _run_worker()


@worker_app.command("run")
def run_command() -> None:
    """Run the singleton loop-timer worker — the cadence owner (#1796)."""
    _run_worker()


def _flock_holder_pid() -> int | None:
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        WORKER_SINGLETON,
        default_pid_path,
        read_pid,
    )

    return read_pid(default_pid_path(WORKER_SINGLETON))


def _resolve_kill_switch() -> tuple[bool, str]:
    """The resolved ``loop_runner_enabled`` value + the tier it came from (env/overlay/global/default)."""
    import os  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    from teatree.config import get_effective_settings  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    value = get_effective_settings().loop_runner_enabled
    if os.environ.get("T3_LOOP_RUNNER_ENABLED", "").strip():
        return value, "env"
    overlay = os.environ.get("T3_OVERLAY_NAME", "").strip()
    if overlay and ConfigSetting.objects.filter(scope=overlay, key="loop_runner_enabled").exists():
        return value, f"overlay:{overlay}"
    if ConfigSetting.objects.filter(scope="", key="loop_runner_enabled").exists():
        return value, "global"
    return value, "default"


def _timer_counts() -> dict[str, dict[str, int]]:
    """Per-loop ``{ready, running}`` ``loop_timer`` chain counts across the enabled set."""
    from teatree.core.models import Loop  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
    from teatree.loops.timer_chains import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        pending_loop_timers,
        running_loop_timers,
    )

    counts: dict[str, dict[str, int]] = {}
    for name in Loop.objects.enabled().values_list("name", flat=True):
        counts[name] = {"ready": len(pending_loop_timers(name)), "running": len(running_loop_timers(name))}
    return counts


@worker_app.command("status")
def status_command(*, json_output: bool = typer.Option(False, "--json", help="Emit the status as JSON.")) -> None:
    """Report the worker: flock holder, kill-switch + tier, timer counts, and whether loops tick.

    Exits NON-ZERO when the loop fleet is stale.
    """
    # The flock, the kill-switch and the READY timer rows all sit BEFORE the admission
    # verdict that decides a tick, so all three read green while nothing happens.
    # ``loop_health`` is the reading that closes that gap, and it is a GATE — a health
    # surface that cannot fail is the one that let a seven-hour freeze look healthy.
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from django.utils import timezone  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    from teatree.loops.loop_staleness import loop_health  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        WORKER_SINGLETON,
        default_pid_path,
        flock_is_held,
    )

    holder = _flock_holder_pid()
    # The pid file can be missing/stale while a worker actually holds the kernel flock
    # (the pid file is diagnostic; the flock is the lock). Fall back to the flock probe
    # so status never prints a false "NOT running" while loops are advancing (#3571).
    flock_held = flock_is_held(WORKER_SINGLETON, pid_path=default_pid_path(WORKER_SINGLETON))
    enabled, source = _resolve_kill_switch()
    timers = _timer_counts()
    running = holder is not None or flock_held
    health = loop_health(timezone.now())

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "running": running,
                    "holder_pid": holder,
                    "flock_held": flock_held,
                    "loop_runner_enabled": enabled,
                    "source": source,
                    "timers": timers,
                    **health.as_json(),
                }
            )
        )
        raise typer.Exit(0 if health.ok else 1)

    if holder is not None:
        state = f"RUNNING (pid {holder})"
    elif flock_held:
        state = "RUNNING (flock held; pid file missing/stale)"
    else:
        state = "NOT running"
    typer.echo(f"worker: {state}")
    typer.echo(f"loop_runner_enabled: {enabled} (from {source})")
    if enabled and not running:
        typer.echo("Worker is enabled but not running — run `t3 worker ensure`.")
    ready_total = sum(c["ready"] for c in timers.values())
    running_total = sum(c["running"] for c in timers.values())
    typer.echo(f"loop timers: {len(timers)} enabled loop(s), {ready_total} READY, {running_total} RUNNING")
    for line in health.lines():
        typer.echo(line)
    if not health.ok:
        raise typer.Exit(1)


#: The actions that mean no worker is running as a result — the non-zero exits.
_ENSURE_FAILURES = ("disabled", "error", "unverified")


def _ensure_worker() -> tuple[str, str]:
    """The shared ensure body: kill-switch gate → flock probe → the ONE detached spawner.

    Returns the ``(action, detail)`` pair both ``ensure`` and ``restart`` report, so the
    two can never diverge on when a worker may be spawned.
    """
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        WORKER_SINGLETON,
        flock_is_held,
    )
    from teatree.utils.worker_spawn import spawn_detached_worker  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    enabled, _source = _resolve_kill_switch()
    if not enabled:
        return "disabled", "loop_runner_enabled is OFF — the kill-switch stops loops"
    if flock_is_held(WORKER_SINGLETON):
        return "already-running", "a worker already holds the flock"
    if not spawn_detached_worker():
        return "error", "`t3` not found on PATH"
    return "spawned", "spawned a detached worker"


def _unverified_detail(waited_seconds: float) -> str:
    """Why a spawn cannot be called a success — with the crashed child's own output."""
    from teatree.utils.worker_spawn import read_spawn_log_tail  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    reason = (
        f"no worker holds the flock after {waited_seconds:.0f}s — the fresh worker crashed on "
        "startup or is still booting. Check `t3 worker status`."
    )
    tail = read_spawn_log_tail()
    return f"{reason}\nlast worker output:\n{tail}" if tail else reason


@worker_app.command("ensure")
def ensure_command(
    *,
    start_timeout: float = typer.Option(60.0, "--start-timeout", help="Seconds to wait for the spawned worker."),
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Spawn a detached worker iff ``loop_runner_enabled`` is ON and the flock is free.

    Refuses (with the reason) when the kill-switch is OFF or a worker already holds the
    flock — an idempotent, cheap "make sure one is running" verb for a fresh install or
    a headless box, sharing the ONE spawner with the SessionStart supervisor.

    A spawn is only reported as such once the worker ACTUALLY holds the flock: the
    spawner itself returns success as soon as the ``t3`` binary resolves, so a startup
    crash would otherwise read as a healthy start. When the flock stays free the child's
    captured stderr is printed and the command exits non-zero.
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from teatree.loop.worker_lifecycle import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        wait_for_new_holder,
    )

    action, detail = _ensure_worker()
    if action == "spawned":
        started = wait_for_new_holder(previous_pid=None, timeout=start_timeout)
        if not started.started:
            action, detail = "unverified", _unverified_detail(started.waited_seconds)
        else:
            detail = f"{detail} (pid {started.holder_pid} holds the flock)"
    _emit_ensure(json_output=json_output, action=action, detail=detail)
    if action in _ENSURE_FAILURES:
        raise SystemExit(1)


#: Exit code for a drain that hit its grace window with work still in flight —
#: distinct from 0 (drained) and from ensure's 1/2, so deploy.sh can tell the two
#: apart and proceed knowing a stuck task re-queues via its lease lapse.
_GRACE_EXCEEDED_EXIT = 3


@worker_app.command("drain")
def drain_command(
    *,
    timeout: int = typer.Option(1800, "--timeout", help="Grace seconds to wait for in-flight tasks to finish."),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds between in-flight checks."),
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Quiesce the worker and wait for in-flight tasks to finish (drain-then-deploy).

    Sets ``worker_quiescing`` ON so the claim/admission path admits ZERO new work,
    then waits up to ``--timeout`` seconds for every live CLAIMED lease to clear —
    the supervisor is never stopped and no in-flight sub-agent is killed. Exits 0
    when the worker is drained; exits ``_GRACE_EXCEEDED_EXIT`` (naming the still-
    CLAIMED task pks) when the grace lapses, so a deploy can proceed knowing a stuck
    task re-queues via its lease lapse.

    THE WORKER IS LEFT QUIESCED: this command stops nothing, and the gate it sets is
    cleared only by a fresh container boot (``deploy/entrypoint.sh``). On a bare host
    nothing clears it, so the box keeps running while admitting no work until you run
    `t3 <overlay> config_setting set worker_quiescing false` or `t3 worker restart`.
    To end the worker rather than merely quiesce it, use `t3 worker stop`.
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
    from teatree.loop.drain import DrainOutcome, drain_worker  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    report = drain_worker(timeout=timeout, poll_interval=poll_interval)
    quiescing = worker_is_quiescing()
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "outcome": report.outcome.value,
                    "waited_seconds": round(report.waited_seconds, 3),
                    "still_claimed": report.still_claimed,
                    "worker_quiescing": quiescing,
                }
            )
        )
    else:
        if report.outcome is DrainOutcome.DRAINED:
            typer.echo(f"drained: no in-flight tasks after {report.waited_seconds:.0f}s — safe to deploy")
        else:
            pks = ", ".join(str(pk) for pk in report.still_claimed) or "(none listed)"
            typer.echo(
                f"grace-exceeded: {len(report.still_claimed)} task(s) still CLAIMED after "
                f"{report.waited_seconds:.0f}s: {pks}"
            )
        if quiescing:
            typer.echo(_QUIESCED_NOTICE)
    if report.outcome is DrainOutcome.GRACE_EXCEEDED:
        raise SystemExit(_GRACE_EXCEEDED_EXIT)


#: Exit code for a stop/restart that did not reach the state it was asked for — a worker
#: still holding the flock, or a fresh worker that never took it. Distinct from ensure's 1
#: (refused before doing anything) and drain's 3 (quiesced but work still in flight).
_STOP_FAILED_EXIT = 4


@worker_app.command("stop")
def stop_command(
    *,
    drain: bool = typer.Option(
        True, "--drain/--no-drain", help="Quiesce and wait for in-flight tasks before signalling (default)."
    ),
    timeout: int = typer.Option(1800, "--timeout", help="Grace seconds for the drain (ignored with --no-drain)."),
    exit_timeout: float = typer.Option(60.0, "--exit-timeout", help="Seconds to wait for the flock to be released."),
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Stop the running singleton worker gracefully and VERIFY that it exited.

    Drains first (the same ``drain_worker`` `t3 worker drain` runs) so no in-flight
    sub-agent is killed, then SIGTERMs the flock holder — located by the kernel flock
    probe plus the pid the holder recorded under the lock, never by a scan for a
    plausible pid — and waits up to ``--exit-timeout`` for the flock to be RELEASED.
    A worker that does not exit is reported as such, with its pid, and exits non-zero.

    ``worker_quiescing`` is always put back exactly as the stop found it, so a failed
    stop can never leave the box admitting nothing; whenever the gate is still ON at
    the end (you had quiesced it yourself before), the output says so and names the
    command that clears it.
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from teatree.loop.worker_lifecycle import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        StopRequest,
        WorkerStopper,
    )

    report = WorkerStopper(StopRequest(drain=drain, drain_timeout=timeout, exit_timeout=exit_timeout)).stop()
    _emit_stop(report, json_output=json_output)
    if not report.worker_gone:
        raise SystemExit(_STOP_FAILED_EXIT)


@worker_app.command("restart")
def restart_command(
    *,
    drain: bool = typer.Option(
        True, "--drain/--no-drain", help="Quiesce and wait for in-flight tasks before signalling (default)."
    ),
    timeout: int = typer.Option(1800, "--timeout", help="Grace seconds for the drain (ignored with --no-drain)."),
    exit_timeout: float = typer.Option(60.0, "--exit-timeout", help="Seconds to wait for the flock to be released."),
    start_timeout: float = typer.Option(60.0, "--start-timeout", help="Seconds to wait for the FRESH worker."),
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Stop the running worker, start a fresh one, and PROVE the new one holds the flock.

    `stop` then the same spawn `t3 worker ensure` uses — and then an independent check,
    because the spawner reports success as soon as the ``t3`` binary exists (the child's
    streams go to ``DEVNULL``, so a startup crash is invisible in its verdict). Success
    is claimed only once the flock is held by a pid that is not the stopped one.

    This is also the one-command recovery from a stuck quiesce: the gate is cleared
    before the fresh worker is spawned, exactly as a container boot's init does.
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
    from teatree.loop.drain import set_worker_quiescing  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
    from teatree.loop.worker_lifecycle import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        StopRequest,
        WorkerStopper,
        wait_for_new_holder,
    )

    stopped = WorkerStopper(StopRequest(drain=drain, drain_timeout=timeout, exit_timeout=exit_timeout)).stop()
    if not stopped.worker_gone:
        _emit_stop(stopped, json_output=json_output)
        raise SystemExit(_STOP_FAILED_EXIT)

    if worker_is_quiescing():
        set_worker_quiescing(value=False)
    action, detail = _ensure_worker()
    if action in _ENSURE_FAILURES:
        _emit_restart(json_output=json_output, action=action, detail=detail, previous_pid=stopped.holder_pid)
        raise SystemExit(1)

    started = wait_for_new_holder(previous_pid=stopped.holder_pid, timeout=start_timeout)
    if not started.started:
        _emit_restart(
            json_output=json_output,
            action="unverified",
            detail=f"{detail}, but {_unverified_detail(started.waited_seconds)}",
            previous_pid=stopped.holder_pid,
        )
        raise SystemExit(_STOP_FAILED_EXIT)
    _emit_restart(
        json_output=json_output,
        action="restarted",
        detail=(
            f"worker pid {stopped.holder_pid} → pid {started.holder_pid} "
            f"(flock re-acquired after {started.waited_seconds:.0f}s)"
        ),
        previous_pid=stopped.holder_pid,
        new_pid=started.holder_pid,
    )


def _emit_ensure(*, json_output: bool, action: str, detail: str) -> None:
    if json_output:
        typer.echo(json.dumps({"action": action, "detail": detail}))
    else:
        typer.echo(f"{action}: {detail}")


def _emit_restart(
    *,
    json_output: bool,
    action: str,
    detail: str,
    previous_pid: int | None,
    new_pid: int | None = None,
) -> None:
    if json_output:
        typer.echo(
            json.dumps({"action": action, "detail": detail, "previous_pid": previous_pid, "holder_pid": new_pid})
        )
    else:
        typer.echo(f"{action}: {detail}")


def _stop_headline(report: "StopReport") -> str:
    from teatree.loop.worker_lifecycle import StopOutcome  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    if report.outcome is StopOutcome.NOT_RUNNING:
        return "not-running: no worker holds the flock — nothing to stop"
    if report.outcome is StopOutcome.STOPPED:
        return f"stopped: worker pid {report.holder_pid} exited after {report.waited_seconds:.0f}s (flock released)"
    if report.outcome is StopOutcome.NO_HOLDER_PID:
        return (
            "still-running: a worker holds the flock but no pid is recorded for it — refusing to guess "
            "which process to signal. Identify it with `ps ax | grep 't3 worker'` and SIGTERM it by hand."
        )
    return (
        f"still-running: pid {report.holder_pid} did not release the flock within "
        f"{report.waited_seconds:.0f}s — it is still alive. Check `t3 worker status`."
    )


def _drain_payload(report: "DrainReport | None") -> DrainPayload | None:
    if report is None:
        return None
    return {"outcome": report.outcome.value, "still_claimed": report.still_claimed}


def _emit_stop(report: "StopReport", *, json_output: bool) -> None:
    from teatree.loop.drain import DrainOutcome  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "outcome": report.outcome.value,
                    "holder_pid": report.holder_pid,
                    "waited_seconds": round(report.waited_seconds, 3),
                    "worker_quiescing": report.quiescing,
                    "drain": _drain_payload(report.drain),
                }
            )
        )
        return

    typer.echo(_stop_headline(report))
    if report.drain is not None and report.drain.outcome is DrainOutcome.GRACE_EXCEEDED:
        pks = ", ".join(str(pk) for pk in report.drain.still_claimed) or "(none listed)"
        typer.echo(
            f"WARNING the drain grace lapsed with {len(report.drain.still_claimed)} task(s) still CLAIMED "
            f"({pks}) — they were signalled mid-flight and re-queue via their lease lapse."
        )
    if report.quiescing:
        typer.echo(_QUIESCED_NOTICE)


__all__ = ["worker_app"]
