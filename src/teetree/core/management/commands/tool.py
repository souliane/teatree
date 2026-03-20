"""Overlay-specific tool commands dispatched via ``t3 <overlay> tool``."""

import os
import subprocess  # noqa: S404

from django_typer.management import TyperCommand, command

from teetree.core.overlay_loader import get_overlay


class Command(TyperCommand):
    @command()
    def run(self, name: str) -> str:
        """Run an overlay tool command by name."""
        overlay = get_overlay()
        for tool_cmd in overlay.get_tool_commands():
            if tool_cmd.get("name") == name:
                mgmt_cmd = tool_cmd.get("management_command", "")
                if not mgmt_cmd:
                    return f"Tool '{name}' has no management_command defined."
                env = {**os.environ}
                env.pop("VIRTUAL_ENV", None)
                subprocess.run(mgmt_cmd, shell=True, check=True, env=env)  # noqa: S602
                return f"Tool '{name}' completed."
        available = [t.get("name", "?") for t in overlay.get_tool_commands()]
        return f"Unknown tool: {name}. Available: {', '.join(available) or 'none'}"

    @command(name="list")
    def list_tools(self) -> str:
        """List available overlay tool commands."""
        overlay = get_overlay()
        tools = overlay.get_tool_commands()
        if not tools:
            return "No tool commands configured in the overlay."
        lines = []
        for t in tools:
            name = t.get("name", "?")
            help_text = t.get("help", "")
            lines.append(f"  {name}: {help_text}" if help_text else f"  {name}")
        return "\n".join(lines)
