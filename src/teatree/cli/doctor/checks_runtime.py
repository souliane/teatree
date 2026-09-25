"""``_check_*`` probes for running-process health invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.run_checks.run_doctor_checks`.
"""

import contextlib
import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Final

import typer

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

RESTART_LOOP_MIN_RESTARTS: Final = 20
RESTART_LOOP_WINDOW_SECONDS: Final = 600
_DOCKER_TIMEOUT_SECONDS: Final = 15
_CONTAINER_ROW_FORMAT: Final = (
    '{{index .Config.Labels "com.docker.compose.service"}}\t{{.RestartCount}}\t{{.State.ExitCode}}'
    "\t{{.State.StartedAt}}\t{{.State.Status}}"
)


def _check_singletons() -> bool:
    """Report a singleton lock file that is stale AND idle (no live flock holder).

    Never unlinks: the lock file is the ``flock`` anchor, so removing one that a
    live worker holds orphans its kernel lock and blinds every later probe (#3617).
    A stale pid alongside a FREE flock is harmless (the next start reuses the file
    in place) — reported for visibility only, not reaped.
    """
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: keeps the doctor-check import light)
        WORKER_SINGLETON,
        default_pid_path,
        flock_is_held,
        read_pid,
    )

    for name in (WORKER_SINGLETON, "slack-listener", "loop-tick"):
        path = default_pid_path(name)
        if path.is_file() and read_pid(path) is None and not flock_is_held(name, pid_path=path):
            typer.echo(f"OK    {name} pid file is stale but idle (reused in place on next start)")
    return True


def _check_worker_running() -> bool:
    """WARN when the active preset admits work but no worker is running.

    A FREE ``worker`` flock means no worker is draining the loop-timer chains — the loops
    are silently dead. Actionable: run ``t3 worker ensure``. Read-only; always returns
    ``True`` (a WARN, not a hard FAIL), and any read error is swallowed so the doctor run
    never crashes on it.
    """
    # A doctor check must never crash the doctor run — any read error is swallowed.
    with contextlib.suppress(Exception):
        from teatree.loops.enable_verdict import fleet_admits_work  # noqa: PLC0415 (deferred: pulls the ORM)
        from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held  # noqa: PLC0415 (deferred: light import)

        if fleet_admits_work() and not flock_is_held(WORKER_SINGLETON):
            typer.echo("WARN  the active preset admits work but no worker holds the flock — run `t3 worker ensure`")
    return True


def _inspect_compose_containers() -> list[str] | None:
    """A ``service, restarts, exit, started, status`` row per compose container; ``None`` if docker cannot answer."""
    try:
        listed = run_allowed_to_fail(
            ["docker", "ps", "--all", "--quiet", "--filter", "label=com.docker.compose.service"],
            expected_codes=None,
            timeout=_DOCKER_TIMEOUT_SECONDS,
        )
        if listed.returncode != 0:
            return None
        ids = listed.stdout.split()
        if not ids:
            return []
        inspected = run_allowed_to_fail(
            ["docker", "inspect", "--format", _CONTAINER_ROW_FORMAT, *ids],
            expected_codes=None,
            timeout=_DOCKER_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutExpired):
        return None
    return inspected.stdout.splitlines() if inspected.returncode == 0 else None


def _restart_loop_finding(row: str, now: dt.datetime) -> str | None:
    service, restarts, exit_code, started_at, status = row.split("\t")
    since_start = int((now - dt.datetime.fromisoformat(started_at)).total_seconds())
    if int(restarts) < RESTART_LOOP_MIN_RESTARTS or int(exit_code) != 0 or since_start > RESTART_LOOP_WINDOW_SECONDS:
        return None
    # Docker zeroes ExitCode when a container starts, so only a stopped one still carries its last exit.
    how = "and is running again, its exit code zeroed by Docker" if status == "running" else "last exit 0"
    return (
        f"FAIL  {service} restarted {restarts} times, {how}, started {since_start}s ago — a restart loop the "
        f"watchdog cannot see; `t3 loop preset show` names the posture, `docker compose logs {service}` why it exits"
    )


def _check_clean_exit_restart_loop(*, inspect: Callable[[], list[str] | None] = _inspect_compose_containers) -> bool:
    """FAIL a compose service Docker keeps restarting after a clean exit — the watchdog only revives an exited one."""
    rows = inspect()
    if rows is None:
        typer.echo(
            "INFO  restart-loop check unverifiable here (docker daemon unreachable) — run `t3 doctor check` in "
            "teatree-worker, the service the docker socket is granted to"
        )
        return True
    now = dt.datetime.now(dt.UTC)
    ok = True
    for row in rows:
        try:
            finding = _restart_loop_finding(row, now)
        except ValueError:
            typer.echo(f"INFO  restart-loop check could not read the container row {row!r}")
            continue
        if finding is not None:
            typer.echo(finding)
            ok = False
    return ok


def _holder_findings(*, env: dict[str, str] | None, pid_path: Path | None, refusal_path: Path | None) -> list[str]:
    """Every way the worker singleton's holder disagrees with who should hold it (#3976).

    Two independent facts, each a FAIL on its own. The STREAK is evidence a worker on
    THIS box could not start, whatever the box is. The HOLDER comparison only applies
    where a deployment defines a sanctioned holder — a bare host has no ``TEATREE_ROLE``,
    so its own ``t3 worker`` is exactly who should hold it and there is nothing to judge.
    """
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: keeps the doctor-check import light)
        DEPLOYMENT_WORKER_ROLE,
        WORKER_SINGLETON,
        current_context,
        default_pid_path,
        flock_is_held,
        read_holder,
    )
    from teatree.utils.singleton_refusals import read_streak  # noqa: PLC0415 (deferred: same)

    findings: list[str] = []
    streak = read_streak(WORKER_SINGLETON, path=refusal_path)
    if streak is not None and streak.escalated:
        findings.append(
            f"FAIL  The worker was refused the singleton {streak.count} times running for the SAME reason — "
            "it is being restarted into a race it cannot win, so no worker of this deployment is running. "
            "Free the singleton (`t3 worker stop`, or end the holder outside this runtime) (#3976)."
        )

    context = current_context(env)
    if not context.role:
        return findings

    path = pid_path or default_pid_path(WORKER_SINGLETON)
    if not flock_is_held(WORKER_SINGLETON, pid_path=path):
        return findings

    record = read_holder(path)
    if record is None:
        findings.append(
            f"FAIL  The worker singleton is held but the holder cannot be attributed — {path} records no "
            f"execution context, so it cannot be shown to be this deployment's {DEPLOYMENT_WORKER_ROLE} "
            "service. Identify it (`ps ax | grep 't3 worker'` on the host AND in the container) (#3976)."
        )
    elif record.context.role != DEPLOYMENT_WORKER_ROLE:
        findings.append(
            f"FAIL  The worker singleton is held from OUTSIDE this deployment's {DEPLOYMENT_WORKER_ROLE} "
            f"service: PID {record.pid} in {record.context.describe()}, while this process runs in "
            f"{context.describe()}. The deployed worker cannot start while that holder lives (#3976)."
        )
    return findings


def _check_worker_singleton_holder(
    *,
    env: dict[str, str] | None = None,
    pid_path: Path | None = None,
    refusal_path: Path | None = None,
) -> bool:
    """HARD-FAIL when the worker singleton's holder is not the one supposed to hold it.

    The starvation this catches leaves every other surface green: the flock genuinely
    IS held, the loops genuinely ARE ticking, and the service is Up because it had just
    restarted. Only comparing the holder against the sanctioned one makes the split
    visible. Crash-proof — a failed read degrades to a WARN so a doctor run never
    reddens on the alarm's own failure.
    """
    try:
        findings = _holder_findings(env=env, pid_path=pid_path, refusal_path=refusal_path)
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Worker-singleton holder check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for finding in findings:
        typer.echo(finding)
    return not findings


def _check_ttyd_for_dashboard(env: dict[str, str] | None = None) -> bool:
    """WARN when the admin box serves the dashboard but ``ttyd`` is missing (#3263).

    The dashboard's loopback "Debug session" button spawns a ``ttyd`` terminal
    (``teatree.agents.terminal_launcher.launch_ttyd``, resolved via
    ``shutil.which("ttyd")``). Only the ``admin`` role serves the dashboard, so
    the check flags a missing ``ttyd`` solely when ``TEATREE_ROLE == "admin"`` —
    a worker/init box (or a plain host that never opens the dashboard) is not
    affected. Surfacing-only: always returns ``True`` so it never gates the
    doctor exit code.
    """
    import os  # noqa: PLC0415 — deferred: loaded only when this command runs
    import shutil  # noqa: PLC0415 — deferred: loaded only when this command runs

    resolved_env = env if env is not None else dict(os.environ)
    if resolved_env.get("TEATREE_ROLE") != "admin":
        return True
    if shutil.which("ttyd") is not None:
        return True
    typer.echo(
        "WARN  ttyd is not installed but this box serves the admin dashboard — the "
        "'Debug session' loopback terminal will fail. Install it (`apt install ttyd`)."
    )
    return True
