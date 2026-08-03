"""``t3 notion`` — headless Notion reads and scoped writes.

The `t3` surface agents call instead of touching the Notion API themselves. It
runs on an internal-integration token from the ``pass`` store, so it works in a
cron/headless run where the interactive claude.ai connector does not exist.

Each failure the setup can produce exits with its own code, so an unattended
caller can branch without parsing prose — see
:mod:`teatree.backends.notion.errors` for the table. ``t3 notion doctor <page>``
is the one-shot triage: it separates "no token", "bad token", "not shared with
the integration" and "not a Notion object" against a real page.

There is deliberately NO whole-page write here. ``section replace`` is
block-scoped and archives only the blocks it enumerated as the section's body,
because a whole-page rewrite destroys the block-level comments and discussions
attached to every block it re-creates. The page-level writes a block tree cannot
express — posting a comment, setting a property — live in
:mod:`teatree.cli.notion_page` and are mounted here as ``comment`` and
``property``.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer

from teatree.cli.notion_page import comment_app, property_app
from teatree.cli.notion_support import fail, live_page, notion_client, object_id

if TYPE_CHECKING:  # pragma: no cover — import-time cost stays off the CLI startup path
    from teatree.backends.notion.client import NotionClient
    from teatree.backends.notion.sections import SectionLocator
    from teatree.types import RawAPIDict

notion_app = typer.Typer(
    name="notion",
    no_args_is_help=True,
    help="Headless Notion access (integration token) — read pages/comments/properties, write scoped.",
)

#: A probe shorter than this is punctuation or a list marker, not identifying text.
_MIN_PROBE_LENGTH = 3

section_app = typer.Typer(name="section", no_args_is_help=True, help="The owned-section write primitive.")
notion_app.add_typer(section_app, name="section")
notion_app.add_typer(comment_app, name="comment")
notion_app.add_typer(property_app, name="property")


def _locator(client: "NotionClient", *, heading: str, legacy: list[str] | None) -> "SectionLocator":
    from teatree.backends.notion.sections import SectionLocator  # noqa: PLC0415 — deferred: lazy CLI import

    return SectionLocator(client, canonical=heading, legacy=tuple(legacy or ()))


def _read_body(body_file: Path | None, blocks_file: Path | None) -> tuple[str, "list[RawAPIDict] | None"]:
    """Return the Markdown body, or raw Notion blocks when ``--blocks-file`` is used."""
    if blocks_file is not None and body_file is None:
        return "", json.loads(blocks_file.read_text(encoding="utf-8"))
    if body_file is not None and blocks_file is None:
        return body_file.read_text(encoding="utf-8"), None
    typer.echo("Pass exactly one of --body-file (Markdown) or --blocks-file (raw Notion block JSON).", err=True)
    raise typer.Exit(code=1)


@notion_app.command("whoami")
def whoami(*, overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use.")) -> None:
    """Verify the integration token and print the bot identity pages must be shared with."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        client = notion_client(overlay)
        typer.echo(client.describe_identity())
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
        if comments:
            rendered += "\n\n" + _render_comments(client.list_comments(page_id), as_json=output_json)
    except NotionError as exc:
        raise fail(exc) from exc
    _emit(rendered, out)


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
    output_json: bool = typer.Option(False, "--json", help="Emit the raw comment objects."),
) -> None:
    """List the open (unresolved) comments on a page or block."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        found = notion_client(overlay).list_comments(object_id(page))
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(_render_comments(found, as_json=output_json))


@notion_app.command("append")
def append(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    body_file: Path = typer.Option(None, "--body-file", help="Markdown body to append."),
    blocks_file: Path = typer.Option(None, "--blocks-file", help="Raw Notion block JSON to append."),
) -> None:
    """Append content at the end of a page, then re-fetch to confirm it landed."""
    from teatree.backends.notion.blocks import build_blocks  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionError, NotionWriteNotLandedError  # noqa: PLC0415 — lazy CLI import
    from teatree.backends.notion.markdown import BlockMarkdownRenderer  # noqa: PLC0415 — deferred: lazy CLI import

    markdown, raw_blocks = _read_body(body_file, blocks_file)
    try:
        client = notion_client(overlay)
        page_id = live_page(client, page).page_id
        payload = raw_blocks if raw_blocks is not None else build_blocks(markdown)
        client.append_block_children(page_id, payload)
        rendered = BlockMarkdownRenderer(client.list_block_children).render(client.list_block_children(page_id))
        probe = _append_probe(markdown)
        if probe and probe not in rendered:
            msg = (
                f"the append reported success but page {page_id} does not contain {probe!r} on "
                "re-fetch — treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(f"appended {len(payload)} block(s) to {page_id} (verified by re-fetch)")


@section_app.command("show")
def section_show(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    heading: str = typer.Option(..., "--heading", help="Canonical H2 heading that identifies the owned section."),
    legacy: list[str] = typer.Option(None, "--legacy", help="Older heading string to adopt. Repeatable."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
) -> None:
    """Show the resolved section: which heading matched, and exactly which blocks are its body."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        client = notion_client(overlay)
        locator = _locator(client, heading=heading, legacy=legacy)
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
                "matched_legacy": section.matched_legacy,
                "toggle": section.toggle,
                "heading_block_id": section.heading_id,
                "body_block_ids": list(section.body_block_ids),
            },
            indent=2,
        )
    )


@section_app.command("replace")
def section_replace(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    heading: str = typer.Option(..., "--heading", help="Canonical H2 heading that identifies the owned section."),
    body_file: Path = typer.Option(..., "--body-file", help="Markdown body for the section."),
    legacy: list[str] = typer.Option(None, "--legacy", help="Older heading string to adopt. Repeatable."),
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

    markdown = body_file.read_text(encoding="utf-8")
    try:
        client = notion_client(overlay)
        locator = _locator(client, heading=heading, legacy=legacy)
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
            client.query_data_source(target, db_filter=db_filter)
            if data_source
            else client.query_database(target, db_filter=db_filter)
        )
    except NotionError as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(rows[:limit] if limit else rows, indent=2))


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
    typer.echo(f"token: OK — {identity}")
    try:
        page_id = object_id(page)
        client.get_page(page_id)
    except NotionError as exc:
        typer.echo(f"page:  FAIL — {exc}", err=True)
        raise fail(exc) from exc
    typer.echo("page:  OK — readable by this integration")
    verdict = client.page_liveness(page_id)
    if verdict.readable:
        typer.echo(f"live:  OK — {verdict.detail}")
        return
    typer.echo(f"live:  {verdict.state.value.upper()} — {verdict.detail}", err=True)
    typer.echo(f"       {verdict.recovery()}", err=True)
    raise fail(verdict.as_error(page_id))


def _render_comments(found: "list[RawAPIDict]", *, as_json: bool) -> str:
    if as_json:
        return json.dumps(found, indent=2)
    if not found:
        return "## Comments\n\n(no open discussions)"
    from teatree.backends.notion.markdown import rich_text_plain  # noqa: PLC0415 — deferred: lazy CLI import

    lines = ["## Comments", ""]
    for comment in found:
        rich_text = comment.get("rich_text")
        body = rich_text_plain(cast("list[RawAPIDict]", rich_text)) if isinstance(rich_text, list) else ""
        author = comment.get("created_by")
        author_id = cast("RawAPIDict", author).get("id", "?") if isinstance(author, dict) else "?"
        stamp = f"{author_id} @ {comment.get('created_time', '?')}"
        lines.append(f"- [{comment.get('discussion_id', '?')}] {stamp}: {body}")
    return "\n".join(lines)


def _append_probe(markdown: str) -> str:
    """The text the re-fetch must contain for a Markdown append to count as landed."""
    candidates = [
        line.strip().lstrip("#>-* ") for line in markdown.splitlines() if len(line.strip()) > _MIN_PROBE_LENGTH
    ]
    return candidates[-1] if candidates else ""


def _emit(rendered: str, out: Path | None) -> None:
    if out is None:
        typer.echo(rendered)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {len(rendered):,} chars to {out}")
