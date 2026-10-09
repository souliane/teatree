"""``t3 notion comment`` and ``t3 notion property`` — the page-level write surface.

The two things a headless run needs that the block tree cannot give it: post a
notification comment (once per marker) — on the page, or on the block holding a
quoted span — and read or write a page property.
``fetch`` renders blocks, so it can answer neither.

Both writes verify by re-reading, and both report their outcome as JSON so an
unattended caller branches on a field rather than on prose. Exit codes stay
reserved for conditions a human must act on — an already-posted marker is not
one of them, so it reports ``duplicate`` at exit 0 rather than inventing a
failure out of the desired end state already holding.
"""

import dataclasses
import json
from typing import Annotated

import typer

from teatree.cli.notion_support import TextInput, announce_writer, fail, live_page, notion_client
from teatree.utils.expanded_params import Expand, expand_dataclass_params

comment_app = typer.Typer(name="comment", no_args_is_help=True, help="Post a page comment, once per marker.")
property_app = typer.Typer(name="property", no_args_is_help=True, help="Read or write one page property.")


@comment_app.command("post")
@expand_dataclass_params
def comment_post(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    source: Annotated[TextInput, Expand("body_")],
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

    body = source.read("body")
    try:
        client = notion_client(overlay)
        announce_writer(client)
        result = CommentPoster(client).post(
            live_page(client, page).page_id, body, marker=marker, allow_duplicate=allow_duplicate
        )
    except (NotionError, ValueError) as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(dataclasses.asdict(result), indent=2))


@comment_app.command("on")
@expand_dataclass_params
def comment_on(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    quote: str = typer.Option(..., "--quote", help="Exact text that occurs once on the page; anchors the comment."),
    source: Annotated[TextInput, Expand("body_")],
    marker: str = typer.Option("", "--marker", help="Dedup key to look for first. Defaults to the whole comment."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Open a discussion on the one block whose text contains --quote, quoting it at the top.

    Notion's API anchors a new discussion on a whole block, never on a selected
    range of text inside it, so the quote carries the exact span. The quote must
    occur exactly once (exit 19 otherwise); a marker already on that block
    reports ``duplicate`` and posts nothing.
    """
    from teatree.backends.notion.comments import CommentPoster  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.replace import PageText  # noqa: PLC0415 — deferred: lazy CLI import

    body = f"“{quote}”\n\n{source.read('body').strip()}"
    try:
        client = notion_client(overlay)
        announce_writer(client)
        slot, _start, _slots = PageText(client).locate(live_page(client, page).page_id, quote)
        result = CommentPoster(client).post_on_block(slot.block_id, body, marker=marker)
    except (NotionError, ValueError) as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps({**dataclasses.asdict(result), "block_id": slot.block_id}, indent=2))


@comment_app.command("reply")
@expand_dataclass_params
def comment_reply(
    page: str = typer.Argument(..., help="Page id or notion.so URL the discussion belongs to."),
    *,
    discussion: str = typer.Option(..., "--discussion", help="Discussion id, as `t3 notion comments` lists it."),
    source: Annotated[TextInput, Expand("body_")],
    marker: str = typer.Option("", "--marker", help="Dedup key to look for first. Defaults to the whole reply."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Reply inside an existing discussion on the page — page-level, block or selected-text.

    The discussion must be one the enumeration found under the page — on the
    page, its blocks, its child pages or its embedded databases' rows — (exit 21
    when it is not, 18 when part of the page could not be read). The write guard
    then judges the object the discussion is anchored on, wherever that is.
    A marker, or the whole reply, already in that discussion reports
    ``duplicate`` and posts nothing; a deliberate repeat takes a fresh --marker.
    """
    from teatree.backends.notion.comments import CommentPoster  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.discussions import DiscussionEnumerator  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionDiscussionNotFoundError, NotionError  # noqa: PLC0415 — lazy import

    body = source.read("body")
    try:
        client = notion_client(overlay)
        announce_writer(client)
        page_id = live_page(client, page).page_id
        found = DiscussionEnumerator(client).enumerate(page_id)
        anchor = found.anchor_of(discussion)
        if anchor is None:
            found.raise_if_incomplete()
            msg = f"discussion {discussion} is not an open discussion on page {page_id}. Nothing was posted."
            raise NotionDiscussionNotFoundError(msg)
        result = CommentPoster(client).reply(anchor, discussion, body, marker=marker)
    except (NotionError, ValueError) as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps({**dataclasses.asdict(result), "anchor_id": anchor}, indent=2))


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
        announce_writer(client)
        result = PagePropertyWriter(client).write(live_page(client, page).page_id, name=name, value=value)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps({"outcome": "set", **dataclasses.asdict(result)}, indent=2))
