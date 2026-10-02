"""``t3 notion comment`` and ``t3 notion property`` — the page-level write surface.

The two things a headless run needs that the block tree cannot give it: post a
notification comment (once per marker), and read or write a page property.
``fetch`` renders blocks, so it can answer neither.

Both writes verify by re-reading, and both report their outcome as JSON so an
unattended caller branches on a field rather than on prose. Exit codes stay
reserved for conditions a human must act on — an already-posted marker is not
one of them, so it reports ``duplicate`` at exit 0 rather than inventing a
failure out of the desired end state already holding.
"""

import dataclasses
import json
from pathlib import Path

import typer

from teatree.cli.notion_support import fail, live_page, notion_client

comment_app = typer.Typer(name="comment", no_args_is_help=True, help="Post a page comment, once per marker.")
property_app = typer.Typer(name="property", no_args_is_help=True, help="Read or write one page property.")


@comment_app.command("post")
def comment_post(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    body_file: Path = typer.Option(..., "--body-file", help="File holding the comment text (stored verbatim)."),
    marker: str = typer.Option("", "--marker", help="Dedup key to look for first. Defaults to the whole body."),
    allow_duplicate: bool = typer.Option(
        False, "--allow-duplicate", help="Post even when the marker is already on the page."
    ),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Post a comment unless its marker is already in the page's open discussions.

    Refusing is the default because the callers are dedup-driven: a skill that
    forgets a flag must under-post, never double-post. ``--allow-duplicate`` is
    the deliberate second copy.
    """
    from teatree.backends.notion.comments import CommentPoster  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    body = body_file.read_text(encoding="utf-8")
    try:
        client = notion_client(overlay)
        result = CommentPoster(client).post(
            live_page(client, page).page_id, body, marker=marker, allow_duplicate=allow_duplicate
        )
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(dataclasses.asdict(result), indent=2))


@property_app.command("get")
def property_get(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    name: str = typer.Option(..., "--name", help="Property name, exactly as it reads in Notion."),
    output_json: bool = typer.Option(False, "--json", help="Emit the raw property object instead of its plain value."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Print one page property — the poll a block-tree fetch cannot answer."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.properties import page_property, plain_property_value  # noqa: PLC0415 — lazy import

    try:
        client = notion_client(overlay)
        prop = page_property(client.get_page(live_page(client, page).page_id), name)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(prop, indent=2) if output_json else plain_property_value(prop))


@property_app.command("set")
def property_set(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    name: str = typer.Option(..., "--name", help="Property name, exactly as it reads in Notion."),
    value: str = typer.Option(..., "--value", help="Literal value; empty clears a nullable property."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Write one page property, shaped by its own type and verified by re-read."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.properties import PagePropertyWriter  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        client = notion_client(overlay)
        result = PagePropertyWriter(client).write(live_page(client, page).page_id, name=name, value=value)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps({"outcome": "set", **dataclasses.asdict(result)}, indent=2))
