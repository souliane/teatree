"""``t3 notion replace`` — change one exactly-located text span on an internal page."""

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Annotated

import typer

from teatree.cli.notion_support import TextInput, announce_writer, fail, live_page, notion_client
from teatree.utils.expanded_params import Expand, expand_dataclass_params

if TYPE_CHECKING:
    from teatree.backends.notion.client import NotionClient
    from teatree.backends.notion.replace import PageText, ReplacePlan
    from teatree.types import RawAPIDict


@expand_dataclass_params
def notion_replace(
    page: str = typer.Argument(..., help="Page id or notion.so URL."),
    *,
    old: Annotated[TextInput, Expand("old_")],
    new: Annotated[TextInput, Expand("new_")],
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the located block and the diff; write nothing."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing and write roots to use."),
) -> None:
    """Replace text that occurs exactly once on the page, inside one block, keeping its formatting.

    Each text is given as a file or inline — exactly one of ``--old-file`` /
    ``--old-text`` and of ``--new-file`` / ``--new-text``. The match is exact and
    case-sensitive against each block's text (a file's one trailing newline is
    dropped; inline text is verbatim, so a host path is never needed where
    ``t3`` runs in a container). Prints ONE JSON document on stdout, a dry run
    included, and the human diff on stderr. Zero or several matches, a
    match spanning blocks, or one crossing differently formatted runs exits 19
    and writes nothing. A block edited between the read and the write exits 20
    and is not overwritten. A match that already sits inside the new text is
    reported ``already applied`` and nothing is written. Pages outside the
    write roots, and pages shared with customers, are refused (exit 17), on a
    dry run too. The page is re-read after the write; every write that may
    have landed is recorded in the outbound-claim ledger — ``verified``, ``not
    landed`` (exit 9) or ``unverified`` (exit 22: a 5xx or a broken connection
    after the request went out, or a failed re-read). A write Notion refused
    (429, 4xx) keeps its own exit code and leaves no row.
    """
    import httpx  # noqa: PLC0415 — deferred: lazy CLI import

    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.replace import PageText  # noqa: PLC0415 — deferred: lazy CLI import

    old_text, new_text = _anchor_text(old, name="old"), _anchor_text(new, name="new")
    if not old_text:
        typer.echo("The old text is empty; there is nothing to anchor the replace on.", err=True)
        raise typer.Exit(code=1)
    if old_text == new_text:
        typer.echo("The old and new text are the same; there is nothing to replace.", err=True)
        raise typer.Exit(code=1)
    try:
        client = notion_client(overlay)
        replacer = PageText(client)
        plan = replacer.plan(live_page(client, page).page_id, old=old_text, new=new_text)
        client.check_writable(plan.slot.block_id)
        result = _dry_run_result(plan) if dry_run else _write_result(replacer, plan)
    except (NotionError, httpx.HTTPError) as exc:
        raise fail(exc) from exc
    typer.echo(json.dumps(result, indent=2))


def _anchor_text(anchor: TextInput, *, name: str) -> str:
    value = anchor.read(name)
    return value.removesuffix("\n").removesuffix("\r") if anchor.file is not None else value


def _dry_run_result(plan: "ReplacePlan") -> "RawAPIDict":
    typer.echo(f"dry run — nothing written\npage:  {plan.page_id}\nblock: {plan.slot.label}\n{plan.diff()}", err=True)
    return {"outcome": "dry_run", "already_applied": plan.already_applied, **replace_preview(plan)}


def _write_result(replacer: "PageText", plan: "ReplacePlan") -> "RawAPIDict":
    if plan.already_applied:
        return replace_result(plan, "already applied", plan.expected_counts)
    counts = apply_replace(replacer, plan, identity=announce_writer(replacer.client))
    return replace_result(plan, "replaced", counts)


def replace_preview(plan: "ReplacePlan") -> "RawAPIDict":
    return {
        "page_id": plan.page_id,
        "block_id": plan.slot.block_id,
        "cell": plan.slot.cell,
        "block_label": plan.slot.label,
        "before": plan.before,
        "after": plan.after,
        "diff": plan.diff(),
    }


def replace_result(plan: "ReplacePlan", outcome: str, counts: tuple[int, int]) -> "RawAPIDict":
    return {
        "outcome": outcome,
        "page_id": plan.page_id,
        "block_id": plan.slot.block_id,
        "old_occurrences_after": counts[0],
        "new_occurrences_after": counts[1],
    }


def apply_replace(replacer: "PageText", plan: "ReplacePlan", *, identity: "RawAPIDict") -> tuple[int, int]:
    """Write the plan and record it; a write that may have landed is recorded too, and never with page text."""
    from teatree.backends.notion.errors import (  # noqa: PLC0415 — deferred: lazy CLI import
        NotionWriteNotLandedError,
        NotionWriteUnverifiedError,
    )

    actor = f"{identity.get('name', '?')} ({identity.get('id', '?')})"
    try:
        counts = replacer.apply(plan)
    except NotionWriteNotLandedError:
        _record_edit(replacer.client, plan, actor=actor, outcome="not landed")
        raise
    except NotionWriteUnverifiedError:
        _record_edit(replacer.client, plan, actor=actor, outcome="unverified")
        raise
    _record_edit(replacer.client, plan, actor=actor, outcome="verified")
    return counts


_DRIFT_REASONS = {
    "not landed": "the re-read after the write did not show the planned text and formatting",
    "unverified": "the write may have landed but its response or the re-read failed; the page state is unknown",
}


def _record_edit(client: "NotionClient", plan: "ReplacePlan", *, actor: str, outcome: str) -> None:
    from teatree.outbound_claim import record_claim  # noqa: PLC0415 — deferred: needs apps

    before_sha = hashlib.sha256(plan.before.encode()).hexdigest()[:16]
    after_sha = hashlib.sha256(plan.after.encode()).hexdigest()[:16]
    claim = record_claim(
        kind="notion_edit",
        idempotency_key=f"notion_edit:{plan.slot.block_id}:{plan.slot.cell}:{before_sha}:{after_sha}:{uuid.uuid4().hex}",
        target_url=f"https://www.notion.so/{plan.page_id.replace('-', '')}#{plan.slot.block_id.replace('-', '')}",
        extra={
            "overlay": client.overlay or "",
            "page_id": plan.page_id,
            "block_id": plan.slot.block_id,
            "cell": plan.slot.cell,
            "old_sha256": hashlib.sha256(plan.old.encode()).hexdigest(),
            "old_length": len(plan.old),
            "new_sha256": hashlib.sha256(plan.new.encode()).hexdigest(),
            "new_length": len(plan.new),
            "actor": actor,
            "outcome": outcome,
        },
    )
    if claim is None:
        return
    if outcome in _DRIFT_REASONS:
        claim.record_drift(_DRIFT_REASONS[outcome])
    else:
        claim.mark_verified()
