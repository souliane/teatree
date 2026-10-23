"""CLI reference doc generation over the command tree and delegated management help.

The Typer/Click introspection (``build_cli_reference_from_app`` / ``command_paths``
/ ``command_groups``) moved INTO the ``teatree.cli`` module
(:mod:`teatree.cli.command_tree`) so ``teatree.cli`` itself can build the #550
skill-command-validity registry from its own app without a backwards edge. This
module re-exports them on a forward edge for the doc generator, the
``generate-cli-reference`` hook, and the static-invocation tests — the public
import path (``from teatree.cli_reference import command_paths``) is unchanged.

``settings compare`` forwards options to its Django command, so its reference help
comes from that command's Typer app instead of the argument-free forwarding leaf.

See: souliane/teatree#67, souliane/teatree#550.
"""

from typing import Any, cast

import typer
from django.core.management import get_commands, load_command_class

from teatree.cli.command_tree import build_cli_reference_from_app, command_groups, command_paths, render_help_blocks
from teatree.cli.command_tree import render_cli_reference_deterministic as _render_cli_reference_deterministic


def render_cli_reference_deterministic(app: typer.Typer, *, base_name: str = "t3") -> str:
    markdown = _render_cli_reference_deterministic(app, base_name=base_name)
    app_label = get_commands()["settings_compare"]
    command = cast("Any", load_command_class(app_label, "settings_compare"))
    help_markdown = render_help_blocks(command.typer_app, [[]], base_name="t3 settings compare")
    help_text = help_markdown.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    help_text = help_text.split("╭─ Django ", 1)[0].rstrip("\n")
    heading = "#### `t3 settings compare`\n\n```\n"
    before, rest = markdown.split(heading, 1)
    _, after = rest.split("\n```\n", 1)
    return f"{before}{heading}{help_text}\n```\n{after}"


__all__ = [
    "build_cli_reference_from_app",
    "command_groups",
    "command_paths",
    "render_cli_reference_deterministic",
]
