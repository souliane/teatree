"""Typer/Click command-tree introspection — the SSOT for "is ``t3 …`` real".

Walks a Typer app's command tree in-process (no subprocess). Lives INSIDE the
``teatree.cli`` module (beside the app and the overlay proxies it resolves)
because both consumers need it from here:

*   ``teatree.cli`` itself registers the #550 skill-command-validity registry
    provider — it must build the ``(command_paths, command_groups)`` registry
    from its own assembled app, and a lower CLI submodule importing back up into
    ``teatree.cli`` would be a cycle. Housing the introspection here keeps that
    an intra-module call.
*   ``teatree.cli_reference`` (the CLI-reference doc generator) re-exports these
    on a forward edge for the doc/hook/test callers.

See: souliane/teatree#67, souliane/teatree#550.
"""

import contextlib
import importlib
import io

import click
import typer
from typer.main import get_command

from teatree.cli.overlay import OVERLAY_PROXY_COMMANDS


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


def command_paths(app: typer.Typer, *, base_name: str = "t3") -> set[str]:
    """Every resolvable command path in *app*, e.g. ``{"t3 loop tick", …}``.

    The SSOT for "is ``t3 <sub> …`` a real command" — used by the skill
    static-invocation validator (#550) so a renamed/removed subcommand
    cited in a SKILL.md fails CI instead of misleading an agent. Includes
    every group node too (a bare ``t3 loop`` is a valid no-args-is-help
    invocation), mirroring the markdown walker's traversal.
    """
    paths: set[str] = set()

    def _collect(cmd: click.Command, parts: list[str], parent_ctx: click.Context | None) -> None:
        real = _resolve_proxy_leaf(cmd)
        if real is not None:
            cmd = real
        ctx = click.Context(cmd, info_name=parts[-1], parent=parent_ctx)
        paths.add(" ".join(parts))
        if isinstance(cmd, click.Group):
            for sub_name in cmd.list_commands(ctx):
                sub_cmd = cmd.get_command(ctx, sub_name)
                if sub_cmd is not None:
                    _collect(sub_cmd, [*parts, sub_name], parent_ctx=ctx)

    _collect(get_command(app), [base_name], parent_ctx=None)
    return paths


def command_groups(app: typer.Typer, *, base_name: str = "t3") -> set[str]:
    """Subset of :func:`command_paths` whose nodes are click groups.

    A token after a group must be one of the group's children (else it
    is a typo'd subcommand, not an argument). Leaf commands take free
    args, so anything after a leaf is not drift. The validator needs the
    group/leaf distinction to tell ``t3 loop tick <arg>`` (fine) from
    ``t3 loop tickk`` (drift) — ``loop`` is a group, ``tickk`` is not its
    child, so it must fail.
    """
    groups: set[str] = set()

    def _collect(cmd: click.Command, parts: list[str], parent_ctx: click.Context | None) -> None:
        real = _resolve_proxy_leaf(cmd)
        if real is not None:
            cmd = real
        ctx = click.Context(cmd, info_name=parts[-1], parent=parent_ctx)
        if isinstance(cmd, click.Group):
            groups.add(" ".join(parts))
            for sub_name in cmd.list_commands(ctx):
                sub_cmd = cmd.get_command(ctx, sub_name)
                if sub_cmd is not None:
                    _collect(sub_cmd, [*parts, sub_name], parent_ctx=ctx)

    _collect(get_command(app), [base_name], parent_ctx=None)
    return groups


def _get_help_text(cmd: click.Command, ctx: click.Context) -> str:
    """Get help text, capturing stdout for Typer/Rich commands."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = cmd.get_help(ctx)
    captured = buf.getvalue().strip()
    return result if result and result.strip() else captured


def _resolve_proxy_leaf(cmd: click.Command) -> click.Command | None:
    """Return the underlying Django TyperCommand's click leaf for an overlay proxy.

    Overlay leaves (``t3 teatree worktree provision`` etc.) are proxies that forward
    ``--help`` to ``manage.py``; their click object has no real options.  When
    the proxy marker is present, swap it for the TyperCommand's own click tree
    so the doc renders the real flags.  Returns ``None`` on any import failure
    — the walker falls back to the proxy's stub help.
    """
    callback = getattr(cmd, "callback", None)
    name = getattr(callback, "__name__", "")
    proxy = OVERLAY_PROXY_COMMANDS.get(name) if name else None
    if proxy is None:
        return None
    group_name, sub_name = proxy
    try:
        module = importlib.import_module(f"teatree.core.management.commands.{group_name}")
        command_cls = module.Command
        typer_app = command_cls.typer_app
    except (ImportError, AttributeError):
        return None
    real_root = get_command(typer_app)
    if not isinstance(real_root, click.Group):
        return real_root
    real_ctx = click.Context(real_root, info_name=group_name)
    return real_root.get_command(real_ctx, sub_name)


def _walk(
    cmd: click.Command,
    parts: list[str],
    lines: list[str],
    depth: int,
    parent_ctx: click.Context | None,
) -> None:
    real = _resolve_proxy_leaf(cmd)
    if real is not None:
        cmd = real

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
