"""``t3 teatree availability`` — manage the 24/7 dual question-mode (#58, §17.3 C3).

Three subcommands manipulate the durable override file that takes
priority over the cron schedule (BLUEPRINT §17.1 invariant 9 / §5.6.3):

* ``t3 teatree availability away [--until ISO8601]`` — force the agent into
    away-mode (deferred questions) until the optional expiry.
* ``t3 teatree availability present [--until ISO8601]`` — force the agent
    into present-mode (interactive questions).
* ``t3 teatree availability auto`` — clear the override; the cron schedule
    decides again.

The command also prints the current resolution (mode + source) so
the user can confirm the effect.
"""

from datetime import UTC, datetime
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.availability import MODE_AWAY, MODE_PRESENT, clear_override, resolve_mode, write_override


def _parse_until(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        msg = f"--until must be ISO8601 (e.g. 2026-05-19T18:00:00+02:00), got {raw!r}: {exc}"
        raise typer.BadParameter(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _render(prefix: str = "") -> str:
    resolution = resolve_mode()
    line = f"availability: mode={resolution.mode} source={resolution.source}"
    return f"{prefix}{line}" if prefix else line


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 teatree availability`` group root."""

    @command()
    def away(
        self,
        until: Annotated[
            str,
            typer.Option(help="ISO8601 timestamp when the override expires (e.g. 2026-05-19T18:00:00+02:00)."),
        ] = "",
    ) -> str:
        """Force away-mode (deferred questions) until *until* — or forever."""
        write_override(MODE_AWAY, until=_parse_until(until))
        return _render(prefix="set away. ")

    @command()
    def present(
        self,
        until: Annotated[
            str,
            typer.Option(help="ISO8601 timestamp when the override expires."),
        ] = "",
        user_id: Annotated[
            str,
            typer.Option("--user-id", help="Slack user id for the away→present backlog drain (defaults to config)."),
        ] = "",
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the drain (per-overlay bot routing)."),
        ] = "",
    ) -> str:
        """Force present-mode (interactive questions) until *until* — or forever.

        Coming back from away auto-drains the deferred-question backlog to
        the user's Slack DM (handled in :func:`write_override`), so the user
        is re-asked everything they missed without any manual step.
        """
        write_override(MODE_PRESENT, until=_parse_until(until), user_id=user_id, overlay=overlay)
        return _render(prefix="set present. ")

    @command()
    def auto(self) -> str:
        """Clear the manual override; the cron schedule decides again."""
        removed = clear_override()
        prefix = "cleared override. " if removed else "no override set. "
        return _render(prefix=prefix)

    @command()
    def show(self) -> str:
        """Print the current resolved mode and which layer decided it."""
        return _render()
