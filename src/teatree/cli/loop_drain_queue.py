"""``t3 loop drain-queue`` subcommands — the reactive DB-queue drain ``/loop`` slot.

Split out of ``teatree.cli.loop`` so that file stays under the module-health
public-function cap: this is the reactive, mechanical DB-queue drain loop's CLI
surface (``run`` / ``status`` / ``start``), mirroring the ``slack-answer`` /
``self-improve`` subapps' shape. The assembled :data:`drain_queue_app` is imported
back by ``teatree.cli.loop`` and registered via
``loop_app.add_typer(..., name="drain-queue")``.
"""

import typer

from teatree.loop.loop_cadences import reactive_slot
from teatree.utils.django_bootstrap import ensure_django

drain_queue_app = typer.Typer(
    name="drain-queue",
    help=(
        "Reactive DB-queue drain loop — a `/loop` slot that keeps the django-tasks "
        "DB queue advancing without an always-on `db_worker`. Runs on a tight "
        "cadence (default 30s) on the `loop-drain-queue` LoopLease: it retires "
        "stale READY jobs, then drains a bounded batch of the fresh remainder, and "
        "stands down while a real `db_worker` holds the `teatree-worker` singleton."
    ),
    no_args_is_help=True,
)


@drain_queue_app.command("run")
def drain_queue_run_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the cycle report as JSON."),
) -> None:
    """Run one reactive DB-queue drain cycle."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_drain_queue", **kwargs)


@drain_queue_app.command("status")
def drain_queue_status_command() -> None:
    """Show how many READY jobs are waiting in the DB queue."""
    ensure_django()

    from django_tasks.base import TaskResultStatus  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    count = DBTaskResult.objects.filter(status=TaskResultStatus.READY).count()
    if not count:
        typer.echo("DB queue empty — no READY jobs awaiting the next drain cycle.")
        return
    typer.echo(f"{count} READY job(s) awaiting the next reactive drain cycle.")


def _drain_cadence_for_loop_slot() -> str:
    """The drain-queue ``/loop`` cadence token — delegates to the shared reactive-slot seam."""
    return reactive_slot("loop-drain-queue").cadence()


@drain_queue_app.command("start")
def drain_queue_start_command() -> None:
    """Print the ``/loop <cadence>`` slot definition for the drain-queue loop.

    Mirrors ``t3 loop slack-answer start``: prints the slash command the user
    pastes inside the loop-owner Claude Code session to register the reactive
    drain-queue ``/loop`` slot. Override the cadence via ``T3_QUEUE_DRAIN_CADENCE``
    (seconds; floor 10).
    """
    register_command = reactive_slot("loop-drain-queue").loop_directive()
    typer.echo("Run this in your interactive Claude Code session to register the drain-queue loop:")
    typer.echo(f"    {register_command}")
    typer.echo("")
    typer.echo(
        "Override the cadence with `T3_QUEUE_DRAIN_CADENCE=<seconds> t3 loop drain-queue start` "
        "(default 30s, floor 10s)."
    )
    typer.echo("")
    typer.echo(
        "Each cycle retires READY jobs older than the stale threshold, then drains "
        "a bounded batch of the fresh remainder in-process — so enqueued headless "
        "tasks advance with no always-on `db_worker` daemon."
    )


__all__ = ["drain_queue_app"]
