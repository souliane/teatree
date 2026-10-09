"""``t3 notion`` — headless Notion reads and scoped writes.

The `t3` surface agents call instead of touching the Notion API themselves. It
runs on an internal-integration token from the ``pass`` store, so it works the same
in an interactive session and a cron/headless run.

Each failure the setup can produce exits with its own code, so an unattended
caller can branch without parsing prose — see
:mod:`teatree.backends.notion.errors` for the table. ``t3 notion doctor <page>``
is the one-shot triage: it separates "no token", "bad token", "the bot cannot see
this id" (not shared, id gone, or another workspace) and "not a Notion object"
against a real page.

There is deliberately NO whole-page write here. ``section replace`` is
block-scoped and archives only the blocks it enumerated as the section's body,
because a whole-page rewrite destroys the block-level comments and discussions
attached to every block it re-creates. The page-level writes a block tree cannot
express — posting a comment, setting a property — live in
:mod:`teatree.cli.notion_page` and are mounted here as ``comment`` and
``property``. ``create`` (:mod:`teatree.cli.notion_create`) makes a new
child page, and ``replace`` (:mod:`teatree.cli.notion_replace`) edits one
exactly-located span inside one block — neither rewrites a page.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from teatree.cli.notion_create import notion_create
from teatree.cli.notion_page import comment_app, property_app
from teatree.cli.notion_replace import notion_replace
from teatree.cli.notion_setup import notion_setup
from teatree.cli.notion_support import (
    BodyInput,
    TextInput,
    announce_writer,
    fail,
    live_page,
    notion_client,
    object_id,
    page_verdict,
    routing_label,
)
from teatree.utils.expanded_params import Expand, expand_dataclass_params

if TYPE_CHECKING:  # pragma: no cover — import-time cost stays off the CLI startup path
    from teatree.backends.notion.client import NotionClient
    from teatree.backends.notion.discussions import PageDiscussions
    from teatree.backends.notion.sections import SectionLocator

notion_app = typer.Typer(
    name="notion",
    no_args_is_help=True,
    help="Headless Notion access (integration token) — read pages/comments/properties, write scoped.",
)

section_app = typer.Typer(name="section", no_args_is_help=True, help="The owned-section write primitive.")
notion_app.add_typer(section_app, name="section")
notion_app.add_typer(comment_app, name="comment")
notion_app.add_typer(property_app, name="property")
notion_app.command("setup")(notion_setup)
notion_app.command("create")(notion_create)
notion_app.command("replace")(notion_replace)


def _locator(client: "NotionClient", *, heading: str) -> "SectionLocator":
    from teatree.backends.notion.sections import SectionLocator  # noqa: PLC0415 — deferred: lazy CLI import

    return SectionLocator(client, canonical=heading)


@notion_app.command("whoami")
def whoami(*, overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use.")) -> None:
    """Verify the integration token and print the bot identity pages must be shared with."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        client = notion_client(overlay)
        typer.echo(f"{client.describe_identity()} {routing_label(client)}")
    except NotionError as exc:
        raise fail(exc) from exc


@notion_app.command("fetch")
def fetch(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    comments: bool = typer.Option(False, "--comments", help="Append the page's open discussions."),
    output_json: bool = typer.Option(False, "--json", help="Emit the raw block tree instead of Markdown."),
    out: Path = typer.Option(None, "--out", help="Write to this file instead of stdout."),
) -> None:
    """Fetch a page as Markdown (or raw blocks), optionally with its open comments.

    An archived, trashed or unprovable page exits 14 with its own diagnostic
    instead of returning a body that reads exactly like a live one. To read one
    anyway for a genuine audit, use the separate ``audit-fetch`` command.
    """
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.markdown import BlockMarkdownRenderer  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        client = notion_client(overlay)
        page_id = live_page(client, page).page_id
        blocks = client.list_block_children(page_id)
        rendered = (
            json.dumps(blocks, indent=2)
            if output_json
            else BlockMarkdownRenderer(client.list_block_children).render(blocks)
        )
        discussions = _enumerate(client, page_id) if comments else None
    except NotionError as exc:
        raise fail(exc) from exc
    if discussions is not None:
        rendered += "\n\n" + _render(discussions, as_json=output_json)
    _emit(rendered, out)
    _refuse_if_incomplete(discussions)


@notion_app.command("audit-fetch")
def audit_fetch(
    page: str = typer.Argument(..., help="Page id or notion.so URL of a page this surface refuses as dead."),
    *,
    reason: str = typer.Option(..., "--reason", help="Why this dead page is being read. Blank does not unblock."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    out: Path = typer.Option(None, "--out", help="Write to this file instead of stdout."),
) -> None:
    """Read a dead page for an AUDIT, never to recover requirements from it.

    Its own command rather than a flag on ``fetch``, deliberately: an escape that
    can be reached by appending a flag to the read you were already typing will be
    reached by habit, and this one must be reached only on purpose. The written
    ``--reason`` is mandatory, the verdict and the reason go to stderr, and the
    Markdown that comes back is stamped, so an audited body can never travel as a
    current source. A page that is genuinely live reads through this command too —
    with no stamp, because there is nothing to warn about.
    """
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.markdown import BlockMarkdownRenderer  # noqa: PLC0415 — deferred: lazy CLI import

    if not reason.strip():
        typer.echo("--reason must carry a written reason; a blank one does not unblock an audit read.", err=True)
        raise typer.Exit(code=1)
    try:
        client = notion_client(overlay)
        live = live_page(client, page, audit_reason=reason)
        rendered = live.stamp + BlockMarkdownRenderer(client.list_block_children).render(
            client.list_block_children(live.page_id)
        )
    except NotionError as exc:
        raise fail(exc) from exc
    _emit(rendered, out)


@notion_app.command("comments")
def comments(
    page: str = typer.Argument(..., help="Page or block id / notion.so URL."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    output_json: bool = typer.Option(False, "--json", help="Emit the structured enumeration."),
    verify: bool = typer.Option(False, "--verify", help="Enumerate twice and report any divergence."),
) -> None:
    """List every open discussion anchored anywhere under a page or block.

    Walks the block tree — a comment's parent is the BLOCK it is anchored to, so
    reading the page anchor alone returns only the page-scoped threads and says
    nothing about the inline ones. Exits 18 when any object could not be read,
    because a shorter list that reads as the whole set is the defect this walk
    exists to remove.
    """
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        found = _enumerate(notion_client(overlay), object_id(page), cross_check=verify)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(_render(found, as_json=output_json))
    _refuse_if_incomplete(found)


@notion_app.command("append")
@expand_dataclass_params
def append(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    body: BodyInput,
    after_heading: str | None = typer.Option(
        None, "--after-heading", help="Insert immediately after this heading's section instead of at the end."
    ),
) -> None:
    """Append content to a page, then re-fetch to confirm it landed.

    Lands at the end unless ``--after-heading`` names a section to follow, which
    exits 15 when the page carries no such heading rather than falling back to
    the end. The re-fetch verification is position-independent either way.
    """
    from teatree.backends.notion.blocks import build_blocks  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.pages import verify_landed  # noqa: PLC0415 — deferred: lazy CLI import

    markdown, raw_blocks = body.read_body()
    try:
        client = notion_client(overlay)
        announce_writer(client)
        page_id = live_page(client, page).page_id
        payload = raw_blocks if raw_blocks is not None else build_blocks(markdown)
        before = len(client.list_block_children(page_id))
        client.append_block_children(page_id, payload, after=_anchor(client, page_id, after_heading))
        verify_landed(client, page_id, markdown=markdown, expected_blocks=len(payload), before=before)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(f"appended {len(payload)} block(s) to {page_id} (verified by re-fetch)")


@section_app.command("show")
def section_show(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    heading: str = typer.Option(..., "--heading", help="Canonical H2 heading that identifies the owned section."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Show the resolved section: which heading matched, and exactly which blocks are its body."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        client = notion_client(overlay)
        locator = _locator(client, heading=heading)
        section = locator.resolve(live_page(client, page).page_id)
    except NotionError as exc:
        raise fail(exc) from exc
    if section is None:
        typer.echo(json.dumps({"outcome": "absent", "heading": heading}, indent=2))
        return
    typer.echo(
        json.dumps(
            {
                "outcome": "present",
                "heading": section.heading_text,
                "toggle": section.toggle,
                "heading_block_id": section.heading_id,
                "body_block_ids": list(section.body_block_ids),
            },
            indent=2,
        )
    )


@section_app.command("replace")
@expand_dataclass_params
def section_replace(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    heading: str = typer.Option(..., "--heading", help="Canonical H2 heading that identifies the owned section."),
    source: Annotated[TextInput, Expand("body_")],
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Rewrite ONE owned section in place — block-scoped, never a whole-page write.

    Absent → created, as a collapsed toggle heading carrying the canonical string.
    There is no ``--blocks-file`` here (the section body must go through the block
    builder for the contract to hold) and no ``--no-create`` (``section show``
    already answers whether the section exists, without writing).
    """
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.sections import SectionWriter  # noqa: PLC0415 — deferred: lazy CLI import

    markdown = source.read("body")
    try:
        client = notion_client(overlay)
        announce_writer(client)
        locator = _locator(client, heading=heading)
        page_id = live_page(client, page).page_id
        section = locator.resolve(page_id)
        writer = SectionWriter(client, locator)
        result = writer.create(page_id, markdown) if section is None else writer.replace(page_id, section, markdown)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(result.__dict__, indent=2))


@notion_app.command("query")
def query(
    database: str = typer.Argument(..., help="Database id, data-source id, or notion.so URL."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    data_source: bool = typer.Option(False, "--data-source", help="Target a data source instead of a database."),
    filter_file: Path = typer.Option(None, "--filter-file", help="JSON file holding a Notion filter object."),
    limit: int = typer.Option(0, "--limit", help="Stop after this many rows (0 = every row)."),
) -> None:
    """Query a Notion database (or data source) and emit the rows as JSON."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    db_filter = json.loads(filter_file.read_text(encoding="utf-8")) if filter_file else None
    try:
        client = notion_client(overlay)
        target = object_id(database)
        rows = (
            client.query_data_source(target, db_filter=db_filter, max_rows=limit)
            if data_source
            else client.query_database(target, db_filter=db_filter, max_rows=limit)
        )
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(rows, indent=2))


@notion_app.command("doctor")
def doctor(
    page: str = typer.Argument(..., help="Page id or notion.so URL to probe reachability for."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Triage one page: token present and valid, page shared, and page still LIVE?

    Reachable is not the same as current, and the third stage is the one a reader
    cannot perform by eye: an archived page answers every earlier stage exactly
    like a live one. It reports ``UNKNOWN`` — never ``OK`` — when the liveness
    could not be established, and exits 14 on anything but ``OK``.
    """
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    # Three stages, reported separately: conflating them is what makes a sharing
    # grant that was never made look like a credential problem, and vice versa.
    try:
        client = notion_client(overlay)
        identity = client.describe_identity()
    except NotionError as exc:
        typer.echo(f"token: FAIL — {exc}", err=True)
        raise fail(exc) from exc
    typer.echo(f"token: OK — {identity} {routing_label(client)}")
    verdict = page_verdict(client, page)
    verdict.echo()
    if verdict.error is not None:
        raise fail(verdict.error)


def _enumerate(client: "NotionClient", object_ref: str, *, cross_check: bool = False) -> "PageDiscussions":
    from teatree.backends.notion.discussions import DiscussionEnumerator  # noqa: PLC0415 — deferred: lazy CLI import

    return DiscussionEnumerator(client).enumerate(object_ref, cross_check=cross_check)


def _render(found: "PageDiscussions", *, as_json: bool) -> str:
    from teatree.backends.notion.discussions import render_discussions  # noqa: PLC0415 — deferred: lazy CLI import

    return render_discussions(found, as_json=as_json)


def _refuse_if_incomplete(found: "PageDiscussions | None") -> None:
    """Exit non-zero AFTER emitting, so the caller keeps what was read and cannot mistake it for the whole set."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    if found is None:
        return
    try:
        found.raise_if_incomplete()
    except NotionError as exc:
        raise fail(exc) from exc


def _anchor(client: "NotionClient", page_id: str, after_heading: str | None) -> str:
    """The sibling block a positioned append chains onto; omitted appends at the end, a supplied blank is refused.

    A toggle heading owns its body as children, so anchoring on the heading block
    itself puts the new content after the whole collapsed section; a plain
    heading's section ends at its last body sibling.
    """
    from teatree.backends.notion.errors import NotionSectionNotFoundError  # noqa: PLC0415 — deferred: lazy CLI import

    if after_heading is None:
        return ""
    if not after_heading.strip():
        msg = (
            "--after-heading must name a heading; an empty value has no position to insert after. Nothing was written."
        )
        raise NotionSectionNotFoundError(msg)
    section = _locator(client, heading=after_heading).resolve(page_id)
    if section is None:
        msg = (
            f"page {page_id} carries no heading matching {after_heading!r}, so there is no position to "
            "insert after. Nothing was written."
        )
        raise NotionSectionNotFoundError(msg)
    if section.toggle:
        return section.heading_id
    return section.body_block_ids[-1] if section.body_block_ids else section.heading_id


def _emit(rendered: str, out: Path | None) -> None:
    if out is None:
        typer.echo(rendered)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {len(rendered):,} chars to {out}")
