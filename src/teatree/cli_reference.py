"""Generate CLI reference docs via Click/Typer introspection.

Walks a Typer app's command tree in-process — no subprocess spawning.
Used by both the ``generate_cli_docs`` management command and the
``generate-cli-reference`` pre-commit hook.

See: souliane/teatree#67
"""

import contextlib
import io

import click
import typer
from typer.main import get_command


def build_cli_reference_from_app(app: typer.Typer, *, base_name: str = "t3") -> str:
    """Walk *app* and return a CLI reference in markdown."""
    click_app = get_command(app)
    lines = [
        "# CLI Reference",
        "",
        f"Generated from `{base_name}` command tree.",
        "",
    ]
    _walk(click_app, [base_name], lines, depth=0, parent_ctx=None)
    return "\n".join(lines) + "\n"


def _get_help_text(cmd: click.Command, ctx: click.Context) -> str:
    """Get help text, capturing stdout for Typer/Rich commands."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = cmd.get_help(ctx)
    captured = buf.getvalue().strip()
    return result if result and result.strip() else captured


def _walk(
    cmd: click.Command,
    parts: list[str],
    lines: list[str],
    depth: int,
    parent_ctx: click.Context | None,
) -> None:
    ctx = click.Context(cmd, info_name=parts[-1], parent=parent_ctx)
    help_text = _get_help_text(cmd, ctx)

    name = " ".join(parts)
    heading = "#" * min(depth + 2, 6)
    lines.extend([f"{heading} `{name}`", "", "```", help_text, "```", ""])

    if isinstance(cmd, click.Group):
        for sub_name in cmd.list_commands(ctx):
            sub_cmd = cmd.get_command(ctx, sub_name)
            if sub_cmd is not None:
                _walk(sub_cmd, [*parts, sub_name], lines, depth + 1, parent_ctx=ctx)
