"""``t3 eval audit`` — run the conversation audit over recent captured sessions (#1861, #2192).

Thin coordinator over the committed engine: sessions are listed by the
production ``claude_sessions`` reader (cwd-project scoped), resolved on disk by
:func:`teatree.eval.transcript_resolver.find_session_file`, paired with a
ground-truth label when a corpus entry's ``source_session_id`` matches, and
audited + persisted by
:func:`teatree.eval.conversation_audit.run_conversation_audit`. ``--confusion
<axis>`` then renders the categorical grid from the persisted ledger
(``--json`` for the machine form). Free and deterministic — no judge, no model
call — and privacy-safe: the table carries ONLY ids, slugs, and categorical
labels, never a payload.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from teatree.claude_sessions import list_sessions
from teatree.eval.corpus_loader import discover_corpus
from teatree.eval.corpus_models import CorpusLabel
from teatree.eval.session_transcript import parse_session_jsonl
from teatree.eval.transcript_resolver import find_session_file
from teatree.utils.django_bootstrap import ensure_django

if TYPE_CHECKING:
    from teatree.core.models import SessionAuditRecord
    from teatree.eval.conversation_audit import AuditInput


def audit(
    limit: int = typer.Option(20, "--limit", help="Audit this many most-recent sessions for the cwd's project."),
    session: str | None = typer.Option(
        None, "--session", help="Audit one specific session id instead of the recent batch."
    ),
    confusion: str | None = typer.Option(
        None,
        "--confusion",
        help="After auditing, render the confusion matrix for this outcome axis from the persisted ledger.",
    ),
    as_json: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False, "--json", help="With --confusion: render the matrix as JSON instead of text."
    ),
) -> None:
    """Audit captured sessions into the durable ledger and print per-session verdicts.

    Each audited session yields one persisted ``SessionAuditRecord`` (verdict,
    categorical triple, nominated-for-label flag); the closing line counts the
    nominations the labelling queue (``t3 eval label nominate``) picks up.
    """
    ensure_django()
    from teatree.core.models import SessionAuditRecord  # noqa: PLC0415 — deferred Django import.
    from teatree.eval.confusion_matrix import (  # noqa: PLC0415 — imports the audit-run model module; deferred with it.
        from_records,
        render_confusion_json,
        render_confusion_text,
    )
    from teatree.eval.conversation_audit import run_conversation_audit  # noqa: PLC0415 — deferred Django import.

    if as_json and confusion is None:
        typer.echo("--json requires --confusion <axis>", err=True)
        raise typer.Exit(code=2)
    inputs = _resolve_inputs(limit=limit, session=session)
    if inputs:
        records = run_conversation_audit(inputs)
        Console().print(_build_audit_table(records))
        nominated = sum(1 for record in records if record.nominated_for_label)
        typer.echo(f"nominated for label: {nominated} (list with `t3 eval label nominate`)")
    else:
        typer.echo("no sessions in scope — nothing audited", err=True)
    if confusion is not None:
        matrix = from_records(confusion, SessionAuditRecord.objects.all())
        typer.echo(render_confusion_json(matrix) if as_json else render_confusion_text(matrix))


def _resolve_inputs(*, limit: int, session: str | None) -> "list[AuditInput]":
    labels = _labels_by_source_session()
    if session is not None:
        path = find_session_file(session)
        if path is None:
            typer.echo(f"no session jsonl found for id {session!r}", err=True)
            raise typer.Exit(code=2)
        return [_audit_input(session, path, labels)]
    inputs: list[AuditInput] = []
    for info in list_sessions(limit=limit):
        path = find_session_file(info.session_id)
        if path is None:
            continue
        inputs.append(_audit_input(info.session_id, path, labels))
    return inputs


def _audit_input(session_id: str, path: Path, labels: dict[str, CorpusLabel]) -> "AuditInput":
    from teatree.eval.conversation_audit import AuditInput  # noqa: PLC0415 — deferred Django import.

    events = parse_session_jsonl(path.read_text(encoding="utf-8", errors="replace"))
    return AuditInput(session_id=session_id, events=events, label=labels.get(session_id))


def _labels_by_source_session() -> dict[str, CorpusLabel]:
    labels = {label.source_session_id: label for label in discover_corpus()}
    labels.pop("", None)
    return labels


def _build_audit_table(records: "list[SessionAuditRecord]") -> Table:
    table = Table(title="Conversation audit", show_lines=False)
    table.add_column("Session", style="bold")
    table.add_column("Entry")
    table.add_column("Axis")
    table.add_column("Expected")
    table.add_column("Predicted")
    table.add_column("Verdict", justify="right")
    table.add_column("Nominated", justify="right")
    for record in records:
        color = "yellow" if record.verdict == "skip" else ("green" if record.verdict == "pass" else "red")
        table.add_row(
            record.session_id,
            record.corpus_entry_id or "—",
            record.outcome_axis,
            record.expected_outcome,
            record.predicted_outcome,
            f"[{color}]{record.verdict}[/{color}]",
            "yes" if record.nominated_for_label else "",
        )
    return table
