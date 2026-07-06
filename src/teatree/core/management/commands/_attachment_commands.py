"""``ticket attachments`` — inspect and fetch a ticket's referenced attachments (PR-15).

Split out of ``ticket.py`` as an :class:`AttachmentCommands` mixin (the same MRO
split as ``TicketShowCommands``) so the cap-bound command god-module does not
grow. ``ticket attachments <ref>`` prints the manifest with each entry marked
fetched / MISSING; ``--fetch`` downloads the missing ones through the manifest's
fetch seams and re-prints. The manifest and gate logic live in
:mod:`teatree.core.intake.attachment_manifest`; this command is the operator surface.
"""

from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.config import worktree_root
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.intake.attachment_manifest import (
    attachments_dir_for,
    build_manifest,
    fetch_manifest,
    ticket_text_sources,
    unfetched_entries,
)
from teatree.core.management.commands._pr_ticket_resolve import resolve_ticket
from teatree.core.models import Ticket


class AttachmentEntryRow(TypedDict):
    source_url: str
    kind: str
    fetched: bool


class AttachmentsResult(TypedDict):
    ticket_id: int
    entries: list[AttachmentEntryRow]
    missing: int


class AttachmentCommands(TyperCommand):
    """The ``ticket attachments`` command, mounted via MRO inheritance (PR-15)."""

    @command()
    def attachments(
        self,
        ticket_ref: str,
        *,
        fetch: Annotated[bool, typer.Option("--fetch", help="Download the missing attachments.")] = False,
    ) -> AttachmentsResult:
        """Print (and with ``--fetch`` download) a ticket's referenced attachments.

        Builds the manifest of every attachment the ticket's issue body/comments
        reference (GitLab uploads, linked Notion files, Slack-thread files) and
        prints each entry as fetched / MISSING. ``--fetch`` downloads the missing
        ones through the manifest's per-source fetch seams and re-prints; a
        source with no wired transport is reported un-fetched with an actionable
        detail rather than silently marked done.
        """
        try:
            ticket = resolve_ticket(ticket_ref)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  No ticket for {ticket_ref!r}")
            raise SystemExit(1) from None

        texts = ticket_text_sources(ticket, code_host=code_host_from_overlay(ticket.overlay or None))
        att_dir = attachments_dir_for(ticket, workspace=worktree_root())

        if fetch:
            manifest, outcomes = fetch_manifest(ticket, texts=texts, attachments_dir=att_dir)
            for outcome in outcomes:
                mark = "fetched" if outcome.ok else "FAILED"
                self.stdout.write(f"  {mark}: {outcome.source_url} — {outcome.detail}")
        else:
            manifest = build_manifest(ticket, texts=texts, attachments_dir=att_dir)

        missing = unfetched_entries(manifest)
        missing_urls = {entry.source_url for entry in missing}
        rows: list[AttachmentEntryRow] = [
            {
                "source_url": str(e["source_url"]),
                "kind": str(e["kind"]),
                "fetched": str(e["source_url"]) not in missing_urls,
            }
            for e in (manifest.entries or [])
        ]
        for row in rows:
            mark = "fetched" if row["fetched"] else "MISSING"
            self.stdout.write(f"  [{mark}] {row['kind']}: {row['source_url']}")
        if missing:
            cmd = f"t3 {ticket.overlay or '<overlay>'} ticket attachments {ticket.pk} --fetch"
            self.stdout.write(f"  {len(missing)} missing — fetch with: {cmd}")
        elif not rows:
            self.stdout.write(f"  ticket {ticket.pk}: no referenced attachments")

        return {"ticket_id": int(ticket.pk), "entries": rows, "missing": len(missing)}
