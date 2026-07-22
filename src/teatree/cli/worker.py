"""``t3 worker`` — the singleton loop-timer worker + its status/ensure controls (#1796 / PR-28).

The worker acquires the ``worker`` flock singleton and runs K pinned ``django_tasks_db``
executor threads that drain the self-rescheduling loop-timer chains (no OS cron /
launchd / systemd). PR-28 flipped ``loop_runner_enabled`` ON by default, so the worker
owns the tick cadence out of the box; the SessionStart supervisor + ``t3 worker ensure``
keep at least one alive. Bare ``t3 worker`` runs the worker (the ``run`` alias — one
documented invocation path); ``status`` and ``ensure`` are the operator controls.
"""

import json

import typer

worker_app = typer.Typer(
    name="worker",
    help=(
        "The singleton loop-timer worker (#1796 / PR-28). Bare `t3 worker` runs it "
        "(the cadence owner, default ON via `loop_runner_enabled`). `status` reports "
        "the live holder + resolved kill-switch; `ensure` spawns a detached worker iff "
        "enabled and the flock is free."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
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
    """Report the worker: the live flock holder, the resolved kill-switch + tier, timer counts."""
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

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
                }
            )
        )
        return

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


@worker_app.command("ensure")
def ensure_command(*, json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON.")) -> None:
    """Spawn a detached worker iff ``loop_runner_enabled`` is ON and the flock is free.

    Refuses (with the reason) when the kill-switch is OFF or a worker already holds the
    flock — an idempotent, cheap "make sure one is running" verb for a fresh install or
    a headless box, sharing the ONE spawner with the SessionStart supervisor.
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        WORKER_SINGLETON,
        flock_is_held,
    )
    from teatree.utils.worker_spawn import spawn_detached_worker  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    enabled, _source = _resolve_kill_switch()
    if not enabled:
        _emit_ensure(
            json_output=json_output,
            action="disabled",
            detail="loop_runner_enabled is OFF — the kill-switch stops loops",
        )
        raise SystemExit(1)
    if flock_is_held(WORKER_SINGLETON):
        _emit_ensure(json_output=json_output, action="already-running", detail="a worker already holds the flock")
        return
    if not spawn_detached_worker():
        _emit_ensure(json_output=json_output, action="error", detail="`t3` not found on PATH")
        raise SystemExit(1)
    _emit_ensure(json_output=json_output, action="spawned", detail="spawned a detached worker")


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
    task re-queues via its lease lapse. The fresh worker's init clears the gate.
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    ensure_django()

    from teatree.loop.drain import DrainOutcome, drain_worker  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    report = drain_worker(timeout=timeout, poll_interval=poll_interval)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "outcome": report.outcome.value,
                    "waited_seconds": round(report.waited_seconds, 3),
                    "still_claimed": report.still_claimed,
                }
            )
        )
    elif report.outcome is DrainOutcome.DRAINED:
        typer.echo(f"drained: no in-flight tasks after {report.waited_seconds:.0f}s — safe to deploy")
    else:
        pks = ", ".join(str(pk) for pk in report.still_claimed) or "(none listed)"
        typer.echo(
            f"grace-exceeded: {len(report.still_claimed)} task(s) still CLAIMED after "
            f"{report.waited_seconds:.0f}s: {pks}"
        )
    if report.outcome is DrainOutcome.GRACE_EXCEEDED:
        raise SystemExit(_GRACE_EXCEEDED_EXIT)


def _emit_ensure(*, json_output: bool, action: str, detail: str) -> None:
    if json_output:
        typer.echo(json.dumps({"action": action, "detail": detail}))
    else:
        typer.echo(f"{action}: {detail}")


__all__ = ["worker_app"]
