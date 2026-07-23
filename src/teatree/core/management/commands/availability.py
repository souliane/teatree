"""``t3 teatree availability`` — deprecated aliases onto the merged Mode (#58, #61).

Availability and loop presets are now ONE concept (:class:`teatree.core.models.Mode`,
the merged *Mode*). These subcommands remain as backward-compatible aliases that set
a ``ModeOverride`` to the corresponding merged mode instead of the retired standalone
availability file:

* ``t3 teatree availability away [--until ISO8601]`` → the holiday ``offline`` mode
    (defer questions AND pause the self-pump).
* ``t3 teatree availability autonomous-away [--until ISO8601]`` → the ``unattended``
    mode (defer questions but KEEP self-pumping).
* ``t3 teatree availability present [--until ISO8601]`` → the ``engaged`` mode
    (interactive questions).
* ``t3 teatree availability auto`` — clear the override; the schedule / default mode
    decides again.

Prefer ``t3 loop preset use <mode>`` / ``t3 loop preset auto`` — the mode IS the
availability. The command prints the resolved mode + source so the effect is clear.
"""

import json
from datetime import UTC, datetime
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.mode_resolution import (
    clear_mode_override,
    mode_name_for_availability,
    resolve_active_mode,
    set_mode_override,
)


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
    resolved = resolve_active_mode()
    line = f"availability: mode={resolved.name} source={resolved.source}"
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
        """Alias: set the holiday ``offline`` mode (defer + pause) until *until* — or forever."""
        set_mode_override(mode_name_for_availability("away"), until=_parse_until(until))
        return _render(prefix="set away. ")

    @command(name="autonomous-away")
    def autonomous_away(
        self,
        until: Annotated[
            str,
            typer.Option(help="ISO8601 timestamp when the override expires (e.g. 2026-05-19T18:00:00+02:00)."),
        ] = "",
    ) -> str:
        """Force autonomous-away — defer questions but KEEP self-pumping (#2544).

        Unlike ``away`` (which also pauses the factory), autonomous-away is the
        unattended-run state: ``AskUserQuestion`` calls defer to the durable
        backlog while the Stop self-pump keeps driving the loop. Alias for the
        ``unattended`` merged mode.
        """
        set_mode_override(mode_name_for_availability("autonomous_away"), until=_parse_until(until))
        return _render(prefix="set autonomous-away. ")

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
        """Alias: set the ``engaged`` present-class mode until *until* — or forever.

        Coming back from an away-class mode auto-drains the deferred-question
        backlog to the user's Slack DM (handled in the mode-override chokepoint),
        so the user is re-asked everything they missed without any manual step.
        """
        set_mode_override(
            mode_name_for_availability("present"), until=_parse_until(until), user_id=user_id, overlay=overlay
        )
        return _render(prefix="set present. ")

    @command()
    def auto(self) -> str:
        """Clear the manual mode override; the schedule / default mode decides again."""
        removed = clear_mode_override()
        prefix = "cleared override. " if removed else "no override set. "
        return _render(prefix=prefix)

    @command()
    def show(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the resolved mode/source as JSON instead of the human line."),
        ] = False,
    ) -> str:
        """Print the current resolved mode and which layer decided it."""
        resolved = resolve_active_mode()
        if json_output:
            return json.dumps({"mode": resolved.name, "source": resolved.source})
        return _render()
