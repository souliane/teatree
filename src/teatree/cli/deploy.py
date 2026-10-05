"""``t3 deploy`` — CLI argument parsing and exit translation for generation rolls."""

import os

import typer
from django.core.management import call_command

from teatree.cli.doctor.deploy_liveness import beating_deploy_record_pid
from teatree.utils.django_bootstrap import ensure_django

deploy_app = typer.Typer(
    name="deploy",
    help="Roll the runtime stack between immutable image generations; `deploy/roll.sh <rev>` runs it.",
    no_args_is_help=True,
)


@deploy_app.callback()
def _deploy_group() -> None:
    """Keep ``roll`` a subcommand even while it is the group's only one."""


@deploy_app.command("roll")
def roll_command(
    *,
    to: str = typer.Option(..., "--to", help="The 40-hex commit sha whose teatree-factory image to roll to."),
    drain_timeout: int = typer.Option(1800, "--drain-timeout", help="Grace seconds for the old generation's claims."),
    verify_timeout: float = typer.Option(300.0, "--verify-timeout", help="Seconds the new generation has to verify."),
    stable_seconds: float = typer.Option(
        30.0, "--stable-seconds", help="Seconds each required service must keep one container start to verify."
    ),
    optional_services: list[str] = typer.Option(
        [],
        "--optional-service",
        help="A service this stack legitimately runs without (e.g. the Slack listener with no Slack overlay).",
    ),
) -> None:
    """Roll the stack to `teatree-factory:<sha>`, restoring the serving generation if anything fails.

    Only `deploy/roll.sh <rev>` runs it: the script holds the deploy lock, keeps the record the watchdog
    stands back for, and names its own pid, which must be the pid that record carries.
    """
    holder = beating_deploy_record_pid()
    if not holder or holder != os.environ.get("TEATREE_ROLL_RECORD_PID", "").strip():
        typer.echo(
            "roll: refused — the deploy lock is not held by the deploy/roll.sh that started this roll. Roll through "
            "`deploy/roll.sh <rev>`, which takes the lock and keeps the record the watchdog stands back for.",
            err=True,
        )
        raise typer.Exit(code=1)
    ensure_django()
    try:
        call_command(
            "deploy_roll",
            to=to,
            drain_timeout=drain_timeout,
            verify_timeout=verify_timeout,
            stable_seconds=stable_seconds,
            optional_service=optional_services,
        )
    except SystemExit as exc:
        raise typer.Exit(code=exc.code if isinstance(exc.code, int) else 1) from exc
