"""``t3 worker`` — the singleton loop-timer worker (#1796).

Acquires the ``worker`` flock singleton and runs K pinned ``django_tasks_db``
executor threads that drain the self-rescheduling loop-timer chains (no OS cron /
launchd / systemd). Enable it first with ``config_setting set loop_runner_enabled
true`` (default OFF); the setting gates whether the worker keeps running. Delegates
to the ``worker`` management command so Django is bootstrapped by the management
framework.
"""

from teatree.utils.django_bootstrap import ensure_django


def worker() -> None:
    """Run the singleton loop-timer worker — the cadence owner (#1796)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    call_command("worker")


__all__ = ["worker"]
