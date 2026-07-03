"""Figma tool commands — direct REST API wrapper for large Figma files.

Split out of ``cli/tools.py`` per the module-health function cap (see
``triage_tools.py``): every command here is a thin front-end over
``teatree.backends.figma.FigmaClient``. Commands register onto the shared
``tool_app`` so ``t3 tool figma-screenshot`` and its siblings form a single
coherent namespace.

Importing this module has the side effect of registering the commands;
``cli/__init__`` imports it after ``tool_app`` is constructed.
"""

import dataclasses
import json
from pathlib import Path

import typer

from teatree.backends.figma import FigmaClient, build_side_by_side_comparison
from teatree.cli.tools import tool_app
from teatree.utils.secrets import read_pass

_TOKEN_PASS_KEY = "figma/pat"  # noqa: S105 — pass key name, not a secret


def _client() -> FigmaClient:
    token = read_pass(_TOKEN_PASS_KEY)
    if not token:
        typer.echo(
            f"No Figma personal access token at `pass show {_TOKEN_PASS_KEY}`. "
            f"Store one with `pass insert {_TOKEN_PASS_KEY}`.",
            err=True,
        )
        raise typer.Exit(code=1)
    return FigmaClient(token=token)


@tool_app.command("figma-screenshot")
def figma_screenshot(
    file_key: str = typer.Argument(..., help="Figma file key (from the file URL)."),
    node_id: str = typer.Argument(..., help="Node/frame ID to render (e.g. `12:34`)."),
    dest: Path = typer.Option(Path("figma-screenshot.png"), "--dest", "-d", help="Output PNG path."),
    scale: float = typer.Option(2.0, "--scale", min=0.01, max=4.0, help="Render scale."),
) -> None:
    """Fetch a Figma node/frame as a PNG — bypasses the MCP integration's size limits."""
    result = _client().get_screenshot(file_key, node_id, dest, scale=scale)
    typer.echo(f"Saved: {result} ({result.stat().st_size:,} bytes)")


@tool_app.command("figma-frames")
def figma_frames(
    file_key: str = typer.Argument(..., help="Figma file key."),
    node_id: str = typer.Argument(..., help="Parent node ID to list children of."),
) -> None:
    """List a node's child frames (name + ID) for navigation."""
    frames = _client().list_frame_children(file_key, node_id)
    if not frames:
        typer.echo("No child frames found.")
        return
    for frame in frames:
        typer.echo(f"{frame.node_id}  {frame.node_type:<12}  {frame.name}")


@tool_app.command("figma-comments")
def figma_comments(
    file_key: str = typer.Argument(..., help="Figma file key."),
    node_id: str = typer.Option("", "--node-id", help="Restrict to comments anchored on this node."),
) -> None:
    """Fetch Figma comments (designer annotations, review feedback) for a file or node."""
    client = _client()
    comments = client.get_node_comments(file_key, node_id) if node_id else client.get_comments(file_key)
    typer.echo(json.dumps(comments, indent=2))


@tool_app.command("figma-components")
def figma_components(
    file_key: str = typer.Argument(..., help="Figma file key."),
) -> None:
    """Fetch component descriptions, variant properties, and styles (design tokens)."""
    metadata = _client().get_component_metadata(file_key)
    typer.echo(json.dumps(dataclasses.asdict(metadata), indent=2))


@tool_app.command("figma-compare")
def figma_compare(
    design_image: Path = typer.Argument(..., help="Figma mockup PNG (e.g. from `figma-screenshot`)."),
    actual_screenshot: Path = typer.Argument(..., help="Playwright screenshot PNG to compare against."),
    dest: Path = typer.Option(Path("figma-comparison.png"), "--dest", "-d", help="Output side-by-side PNG path."),
) -> None:
    """Combine a Figma mockup and a Playwright screenshot side by side for MR evidence."""
    result = build_side_by_side_comparison(design_image, actual_screenshot, dest)
    typer.echo(f"Saved: {result} ({result.stat().st_size:,} bytes)")
