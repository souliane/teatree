"""``t3 loop slack-answer`` subcommands — the third ``/loop`` slot (#1014).

Split out of ``teatree.cli.loop`` so that file stays under the
module-health public-function cap: this is the reactive, token-cheap
Slack-answer loop's CLI surface (``run`` / ``status`` / ``start``),
mirroring the inline ``self-improve`` subapp's shape. The assembled
:data:`slack_answer_app` is imported back by ``teatree.cli.loop`` and
registered via ``loop_app.add_typer(..., name="slack-answer")``.
"""

import os

import typer

from teatree.utils.django_bootstrap import ensure_django

slack_answer_app = typer.Typer(
    name="slack-answer",
    help=(
        "Reactive, token-cheap Slack-answer loop — the third `/loop` slot. "
        "Runs on a tight cadence (default 20s) in the same loop-owner "
        "session as `t3 loop tick`, on a separate LoopLease so a long "
        "answer cycle never blocks a fast regular tick. Complementary to "
        "the inbound prompt-drain, never a double-answer (#1014)."
    ),
    no_args_is_help=True,
)


@slack_answer_app.command("run")
def slack_answer_run_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the cycle report as JSON."),
) -> None:
    """Run one reactive Slack-answer cycle."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_slack_answer", **kwargs)


@slack_answer_app.command("status")
def slack_answer_status_command() -> None:
    """Show the reactive Slack-answer loop's unreplied queue depth."""
    ensure_django()

    from teatree.core.models import PendingChatInjection  # noqa: PLC0415

    count = PendingChatInjection.loop_unreplied().count()
    if not count:
        typer.echo("Slack-answer queue empty — nothing loop-unreplied.")
        return
    typer.echo(f"{count} loop-unreplied Slack message(s) awaiting the next reactive cycle.")


def _slack_answer_cadence_for_loop_slot() -> str:
    """Read ``T3_SLACK_ANSWER_CADENCE`` (seconds, default 20, floor 15)."""
    raw = os.environ.get("T3_SLACK_ANSWER_CADENCE", "20").strip() or "20"
    try:
        seconds = max(15, int(raw))
    except ValueError:
        seconds = 20
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@slack_answer_app.command("start")
def slack_answer_start_command() -> None:
    """Print the ``/loop <cadence>`` slot definition for the Slack-answer loop.

    Mirrors ``t3 loop self-improve start``: prints the slash command the
    user pastes inside the loop-owner Claude Code session to register the
    third ``/loop`` slot. Override the cadence via ``T3_SLACK_ANSWER_CADENCE``
    (seconds; floor 15).
    """
    cadence = _slack_answer_cadence_for_loop_slot()
    register_command = f"/loop {cadence} Run `t3 loop slack-answer run`."
    typer.echo("Run this in your interactive Claude Code session to register the Slack-answer loop:")
    typer.echo(f"    {register_command}")
    typer.echo("")
    typer.echo(
        "Override the cadence with `T3_SLACK_ANSWER_CADENCE=<seconds> t3 loop slack-answer start` "
        "(default 20s, floor 15s)."
    )
    typer.echo("")
    typer.echo(
        "Each cycle reacts :eyes: once per new message, then routes via the "
        "zero-token classifier: ack → reaction, status question → direct "
        "state reply, anything needing work → one bounded t3:answerer task. "
        "Token-cheap and reactive; complementary to the inbound drain (#1014)."
    )


__all__ = ["slack_answer_app"]
