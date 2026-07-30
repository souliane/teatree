"""``_check_*`` probes for running-process health invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.app.run_doctor_checks`.
"""

import contextlib

import typer


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
    """WARN when the loop worker is enabled but not running (PR-28).

    Default-ON ``loop_runner_enabled`` with a FREE ``worker`` flock means no worker is
    draining the loop-timer chains — the loops are silently dead. Actionable: run
    ``t3 worker ensure``. Read-only; always returns ``True`` (a WARN, not a hard FAIL),
    and any read error is swallowed so the doctor run never crashes on it.
    """
    # A doctor check must never crash the doctor run — any read error is swallowed.
    with contextlib.suppress(Exception):
        from teatree.config import get_effective_settings  # noqa: PLC0415 (deferred: light doctor-check import)
        from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held  # noqa: PLC0415 (deferred: light import)

        if get_effective_settings().loop_runner_enabled and not flock_is_held(WORKER_SINGLETON):
            typer.echo("WARN  loop_runner_enabled is ON but no worker holds the flock — run `t3 worker ensure`")
    return True


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
