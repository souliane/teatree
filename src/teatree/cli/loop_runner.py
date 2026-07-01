"""``t3 loop-runner`` — the self-owned singleton loop-runner daemon (#2876).

Acquires the ``loop-runner`` flock singleton and runs the supervised beat daemon
that owns the DB ``Loop`` tick cadence (no OS cron / launchd / systemd). Enable it
first with ``config_setting set loop_runner_enabled true`` (default OFF — the
native Claude ``/loop`` crons drive as today until the operator opts in); the
setting selects the driver, this command runs it. Delegates to the ``loop_runner``
management command so Django is bootstrapped by the management framework.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def loop_runner(
    *,
    once: bool = typer.Option(False, "--once", help="Run a single beat then exit (foreground / test variant)."),
) -> None:
    """Run the supervised singleton loop-runner daemon — the cadence owner (#2876)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, bool] = {"once": True} if once else {}
    call_command("loop_runner", **kwargs)


__all__ = ["loop_runner"]
