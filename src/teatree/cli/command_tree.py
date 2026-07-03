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
import os
import re
from collections.abc import Iterator

import click
import typer
import typer.rich_utils
from typer.main import get_command

from teatree.cli.overlay import OVERLAY_PROXY_COMMANDS

# The render must be byte-identical regardless of the environment it runs in
# (#2599): a fixed console width so the rich help boxes wrap identically on a
# narrow CI runner and a wide local terminal, and no inherited tty / COLUMNS.
_RENDER_WIDTH = 80


def build_cli_reference_from_app(app: typer.Typer, *, base_name: str = "t3") -> str:
    """Walk *app* and return a CLI reference in markdown."""
    with _pinned_render_environment():
        return _build_cli_reference_from_command(get_command(app), base_name=base_name)


def _build_cli_reference_from_command(click_app: click.Command, *, base_name: str = "t3") -> str:
    lines = [
        "# CLI Reference",
        "",
        f"Generated from `{base_name}` command tree.",
        "",
    ]
    _walk(click_app, [base_name], lines, depth=0, parent_ctx=None)
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def _pinned_render_environment() -> Iterator[None]:
    """Force a fixed render width and strip env-derived sizing for the duration.

    rich resolves the console width from ``os.get_terminal_size`` (a tty),
    then ``$COLUMNS``, then a fallback. typer's rich help console honours the
    module-level ``MAX_WIDTH``/``FORCE_TERMINAL`` knobs. Pinning all of these
    makes the help boxes wrap deterministically; the originals are restored on
    exit so this never leaks into the surrounding process.
    """
    saved_max_width = typer.rich_utils.MAX_WIDTH
    saved_force_terminal = typer.rich_utils.FORCE_TERMINAL
    saved_columns = os.environ.get("COLUMNS")
    saved_lines = os.environ.get("LINES")
    typer.rich_utils.MAX_WIDTH = _RENDER_WIDTH
    typer.rich_utils.FORCE_TERMINAL = False
    os.environ["COLUMNS"] = str(_RENDER_WIDTH)
    os.environ.pop("LINES", None)
    try:
        yield
    finally:
        typer.rich_utils.MAX_WIDTH = saved_max_width
        typer.rich_utils.FORCE_TERMINAL = saved_force_terminal
        for key, value in (("COLUMNS", saved_columns), ("LINES", saved_lines)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_HOME_ROOTED_PATH = re.compile(r"^(?:/[^/]+)+/(\.[^/]+)$")
_ABS_HOME_PATH = re.compile(r"/(?:Users|home|root|private|var|tmp)/[^\s\]│]*/(\.[A-Za-z0-9._-]+)")


def _tilde_display(value: object) -> str | None:
    """Return the ``~/<name>`` display for a home-rooted dotfile default, else ``None``.

    Only a path of the shape ``<abs-home-dir>/.<name>`` qualifies — e.g.
    ``Path.home() / ".teatree.toml"``. Folding it to a short ``~/<name>`` string
    BEFORE rich renders it keeps the help box from wrapping/truncating on the
    absolute path's length (which varies by host), so the bytes are stable.
    """
    if not isinstance(value, os.PathLike):
        return None
    match = _HOME_ROOTED_PATH.match(str(value))
    return f"~/{match.group(1)}" if match else None


@contextlib.contextmanager
def _tilde_path_defaults(root: click.Command) -> Iterator[None]:
    """Temporarily render home-rooted dotfile param defaults as ``~/<name>``.

    rich wraps and truncates the ``[default: …]`` cell to the (pinned) box width,
    so an absolute home path that is short on one host but long on another
    produces different bytes — the truncation is baked in at render time, too
    late for a post-render string fold. Rewriting the click param default to a
    short ``~``-prefixed string before rendering makes the cell identical
    everywhere. Originals are restored on exit.
    """
    patched: list[tuple[click.Parameter, object]] = []

    def _visit(cmd: click.Command, ctx: click.Context | None) -> None:
        real = _resolve_proxy_leaf(cmd)
        target = real if real is not None else cmd
        for param in target.params:
            display = _tilde_display(getattr(param, "default", None))
            if display is not None:
                patched.append((param, param.default))
                param.default = display
        if isinstance(target, click.Group):
            sub_ctx = click.Context(target, info_name="t3", parent=ctx)
            for name in target.list_commands(sub_ctx):
                sub = target.get_command(sub_ctx, name)
                if sub is not None:
                    _visit(sub, sub_ctx)

    _visit(root, None)
    try:
        yield
    finally:
        for param, original in patched:
            param.default = original


def _normalize_home_paths(markdown: str) -> str:
    """Backstop fold of any residual absolute home path to ``~`` (#2599).

    ``_tilde_path_defaults`` handles the known home-rooted Option defaults before
    rich can wrap them; this catches any home-rooted dotfile path that slips into
    free-form help prose, so an absolute home path never reaches the public doc.
    """
    return _ABS_HOME_PATH.sub(r"~/\1", markdown)


def render_cli_reference_deterministic(app: typer.Typer, *, base_name: str = "t3") -> str:
    """Render the CLI reference so the bytes are identical across environments.

    The single seam every generator path uses (#2599): pin the render width,
    drop env-derived sizing, fold home-rooted defaults to ``~`` before rich wraps
    them, and backstop-normalize any residual absolute home path.
    """
    click_app = get_command(app)
    with _pinned_render_environment(), _tilde_path_defaults(click_app):
        markdown = _build_cli_reference_from_command(click_app, base_name=base_name)
    markdown = _normalize_home_paths(markdown)
    return "\n".join(line.rstrip() for line in markdown.splitlines()).rstrip("\n") + "\n"


def _resolve_command_path(
    click_app: click.Command, parts: list[str], *, base_name: str
) -> tuple[click.Command, click.Context]:
    """Navigate from the app root to the command named by *parts*.

    Returns the command plus its context chain (each ``info_name`` set) so help
    renders under the right ``t3 <sub> …`` program name. Mirrors the traversal in
    :func:`_walk`, resolving overlay proxies to their real leaf at each hop.
    """
    path = " ".join([base_name, *parts])
    cmd = _resolve_proxy_leaf(click_app) or click_app
    ctx = click.Context(cmd, info_name=base_name)
    for part in parts:
        if not isinstance(cmd, click.Group):
            msg = f"{path}: '{part}' has no subcommands"
            raise KeyError(msg)
        sub = cmd.get_command(ctx, part)
        if sub is None:
            msg = f"{path}: unknown command '{part}'"
            raise KeyError(msg)
        cmd = _resolve_proxy_leaf(sub) or sub
        ctx = click.Context(cmd, info_name=part, parent=ctx)
    return cmd, ctx


def render_help_blocks(app: typer.Typer, paths: list[list[str]], *, base_name: str = "t3") -> str:
    """Render the ``--help`` output of each command path in *paths* deterministically.

    The CLI analog of :func:`render_cli_reference_deterministic` for a CURATED set
    of commands rather than the whole tree: each entry of *paths* is the token list
    under *base_name* (``[]`` → ``t3``, ``["loop"]`` → ``t3 loop``). The bytes are
    identical across environments — the same #2599 seam (pinned width, no env-derived
    sizing, home-rooted dotfile defaults folded to ``~``) the full reference uses.
    """
    click_app = get_command(app)
    sections: list[str] = []
    with _pinned_render_environment(), _tilde_path_defaults(click_app):
        for parts in paths:
            cmd, ctx = _resolve_command_path(click_app, parts, base_name=base_name)
            name = " ".join([base_name, *parts])
            sections.append(f"## `{name}`\n\n```\n{_get_help_text(cmd, ctx)}\n```")
    doc = _normalize_home_paths("\n\n".join(sections))
    return "\n".join(line.rstrip() for line in doc.splitlines()).rstrip("\n") + "\n"


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
