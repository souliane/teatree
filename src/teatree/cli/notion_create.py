"""``t3 notion create`` — a new child page under a page or database the integration already reaches."""

import dataclasses
import json

import typer

from teatree.cli.notion_support import BodyInput, announce_writer, fail, notion_client, object_id
from teatree.utils.expanded_params import expand_dataclass_params


@expand_dataclass_params
def notion_create(
    parent: str = typer.Argument(..., help="Parent page or database id / notion.so URL, shared with the integration."),
    *,
    title: str = typer.Option(..., "--title", help="Title of the new page."),
    body: BodyInput,
    icon: str = typer.Option("", "--icon", help="An emoji for the page icon."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing and write roots to use."),
) -> None:
    """Create a child page, verify its title and body by re-fetch, and print its URL.

    Notion's API does not let an internal integration create a workspace-level
    (top-level private) page, so the parent must already be shared with the
    integration `t3 notion whoami` names, and sit under a write-allowed root.
    A page already titled so under the parent is reported as ``exists`` and
    nothing is written. Runs as the overlay `t3 notion whoami` reports;
    ``--overlay`` (or T3_OVERLAY_NAME) pins another.
    """
    from teatree.backends.notion.blocks import build_blocks  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.pages import PageCreator  # noqa: PLC0415 — deferred: lazy CLI import

    if not title.strip():
        typer.echo("--title must name the page; a blank title is refused.", err=True)
        raise typer.Exit(code=1)
    markdown, raw_blocks = body.read_body()
    try:
        client = notion_client(overlay)
        announce_writer(client)
        blocks = raw_blocks if raw_blocks is not None else build_blocks(markdown)
        created = PageCreator(client).create(
            object_id(parent), title=title.strip(), blocks=blocks, icon=icon, markdown=markdown
        )
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(dataclasses.asdict(created), indent=2))
