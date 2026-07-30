"""CLI reference doc generation — re-exports the command-tree introspection.

The Typer/Click introspection (``build_cli_reference_from_app`` / ``command_paths``
/ ``command_groups``) moved INTO the ``teatree.cli`` module
(:mod:`teatree.cli.command_tree`) so ``teatree.cli`` itself can build the #550
skill-command-validity registry from its own app without a backwards edge. This
module re-exports them on a forward edge for the doc generator, the
``generate-cli-reference`` hook, and the static-invocation tests — the public
import path (``from teatree.cli_reference import command_paths``) is unchanged.

See: souliane/teatree#67, souliane/teatree#550.
"""

from teatree.cli.command_tree import (
    build_cli_reference_from_app,
    command_groups,
    command_paths,
    render_cli_reference_deterministic,
)

__all__ = [
    "build_cli_reference_from_app",
    "command_groups",
    "command_paths",
    "render_cli_reference_deterministic",
]
