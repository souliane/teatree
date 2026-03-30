"""Overlay-specific tool commands dispatched via ``t3 <overlay> tool``."""

import os
import shlex
import subprocess  # noqa: S404

import typer
from django_typer.management import TyperCommand, command

from teatree.core.overlay_loader import get_overlay


class Command(TyperCommand):
    @command(
        context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
    )
    def run(self, ctx: typer.Context, name: str) -> str:
        """Run an overlay tool command by name.

        Extra arguments after the tool name are forwarded to the command.
        """
        extra: list[str] = ctx.args

        overlay = get_overlay()
        for tool_cmd in overlay.metadata.get_tool_commands():
            if tool_cmd.get("name") == name:
                mgmt_cmd = tool_cmd.get("command", "")
                if not mgmt_cmd:
                    return f"Tool '{name}' has no command defined."
                if extra:
                    mgmt_cmd = f"{mgmt_cmd} {shlex.join(extra)}"
                env = {**os.environ}
                env.pop("VIRTUAL_ENV", None)
                subprocess.run(mgmt_cmd, shell=True, check=True, env=env)  # noqa: S602
                return f"Tool '{name}' completed."
        available = [t.get("name", "?") for t in overlay.metadata.get_tool_commands()]
        return f"Unknown tool: {name}. Available: {', '.join(available) or 'none'}"

    @command(name="list")
    def list_tools(self) -> str:
        """List available overlay tool commands."""
        overlay = get_overlay()
        tools = overlay.metadata.get_tool_commands()
        if not tools:
            return "No tool commands configured in the overlay."
        lines = []
        for t in tools:
            name = t.get("name", "?")
            help_text = t.get("help", "")
            lines.append(f"  {name}: {help_text}" if help_text else f"  {name}")
        return "\n".join(lines)
