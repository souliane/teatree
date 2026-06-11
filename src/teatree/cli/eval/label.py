"""``t3 eval label`` — curate ground-truth corpus labels from audit nominations (#2192).

``nominate`` lists the audit records the #1861 engine flagged as
labelling-worthy, ``add`` scaffolds a corpus entry from an audited session, and
``review`` validates that every label loads (``discover_corpus``) and every
matcher oracle is independent (``assert_independent_oracle``) — non-zero exit
on any failure.

``add`` copies the session capture ONLY when the pre-publish privacy scanner
(:func:`teatree.core.gates.privacy_gate.scan_for_publication` — the same
scanner the transcript-fixture conformance tests gate the committed corpus
with) finds no hit: a redact-anchor match refuses loudly and writes nothing,
because the corpus ships inside this public repo. The label template leaves
``labelled_by`` empty on purpose — the ground truth must come from a human who
is not the rule's author, and ``review`` stays red until they fill it in.
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from django.core.exceptions import ImproperlyConfigured
from rich.console import Console
from rich.table import Table

from teatree.core.gates.privacy_gate import scan_for_publication
from teatree.eval.corpus_grade import CircularOracleError, assert_independent_oracle
from teatree.eval.corpus_loader import CORPUS_DIR, discover_corpus
from teatree.eval.corpus_models import CorpusLabel
from teatree.eval.loader import EvalSpecError
from teatree.eval.transcript_resolver import find_session_file
from teatree.utils.django_bootstrap import ensure_django

if TYPE_CHECKING:
    from teatree.core.models import SessionAuditRecord

label_app = typer.Typer(help="Corpus-label curation: list nominations, scaffold a label, review the corpus.")

_DIR_HELP = "Corpus directory (default: the shipped corpus)."
#: The corpus ships inside this public repo, so a copied capture is always
#: scanned as a public-repo publication regardless of overlay config.
_PUBLIC_TARGET = "souliane/teatree"

#: Strong default block patterns for the public-corpus copy path, applied on top
#: of the active overlay's own ``privacy_block_patterns``. A real captured
#: session log routinely carries a host filesystem path or a token-shaped string
#: — either is a permanent PII leak in this public repo — so the guard blocks
#: them by construction even when no overlay terms are configured.
_PUBLIC_CORPUS_BLOCK_PATTERNS: tuple[str, ...] = (
    r"/Users/[^/\s]+",  # macOS host home path (e.g. /Users/alice/...)
    r"/home/[^/\s]+",  # Linux host home path (e.g. /home/alice/...)
    r"\bgh[posru]_[A-Za-z0-9]{20,}",  # GitHub PAT / OAuth / refresh / server token
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",  # GitHub fine-grained PAT
    # Anthropic keys/tokens have a hyphen after `sk-ant`, so the generic
    # `sk-[A-Za-z0-9]{16,}` below (whose char class excludes `-`) stops at the
    # hyphen and never matches the REAL `sk-ant-api03-…` / `sk-ant-oat01-…` shapes.
    # Catch them explicitly first, then keep the generic OpenAI-style fallback.
    r"\bsk-ant-(?:api03|oat01)-[A-Za-z0-9_-]{16,}",  # Anthropic API key / OAuth token
    r"\bsk-[A-Za-z0-9]{16,}",  # OpenAI-style secret key (generic fallback)
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",  # PEM private key block
)


@label_app.command("nominate")
def nominate() -> None:
    """List the audit records nominated for ground-truth labelling."""
    ensure_django()
    from teatree.core.models import SessionAuditRecord  # noqa: PLC0415 — deferred Django import.

    records = list(SessionAuditRecord.objects.nominated())
    if not records:
        typer.echo("(no records nominated for labelling)")
        return
    Console().print(_build_nominated_table(records))
    typer.echo("scaffold one with `t3 eval label add <session-id>`")


@label_app.command("add")
def add(
    session_id: str = typer.Argument(..., help="Session id of an audited session to scaffold into the corpus."),
    entry_id: str | None = typer.Option(
        None, "--entry-id", help="Corpus entry id (default: derived from the session id)."
    ),
    directory: Path | None = typer.Option(None, "--dir", help=_DIR_HELP),
) -> None:
    """Scaffold a corpus entry: copy the session capture and write a label template.

    Refuses (exit 1, nothing written) when the publication privacy scanner finds
    a hit in the capture — a real session log must be redacted before it can
    live in the public corpus. The template pre-fills the categorical fields
    from the session's audit record; ``labelled_by``, ``expected_behavior``, and
    ``expect`` are left for the human labeller, and the printed label path is
    the file to edit.
    """
    ensure_django()
    from teatree.core.models import SessionAuditRecord  # noqa: PLC0415 — deferred Django import.

    record = SessionAuditRecord.objects.for_session(session_id).first()
    if record is None:
        typer.echo(
            f"no audit record for session {session_id!r} — run `t3 eval audit --session {session_id}` first",
            err=True,
        )
        raise typer.Exit(code=2)
    source = find_session_file(session_id)
    if source is None:
        typer.echo(f"no session jsonl found for id {session_id!r}", err=True)
        raise typer.Exit(code=2)
    root = CORPUS_DIR if directory is None else directory
    entry = entry_id or _default_entry_id(session_id)
    session_target = root / f"{entry}.session.jsonl"
    label_target = root / f"{entry}.label.yaml"
    if session_target.exists() or label_target.exists():
        typer.echo(f"corpus entry {entry!r} already exists in {root}", err=True)
        raise typer.Exit(code=2)
    body = source.read_text(encoding="utf-8", errors="replace")
    _refuse_on_redaction_hit(body)
    root.mkdir(parents=True, exist_ok=True)
    session_target.write_text(body, encoding="utf-8")
    label_target.write_text(_label_template(entry, session_id, record), encoding="utf-8")
    typer.echo(f"corpus entry {entry!r} scaffolded:")
    typer.echo(f"  session: {session_target}")
    typer.echo(f"  label:   {label_target}")
    typer.echo("fill labelled_by / expected_behavior / expect, then run `t3 eval label review`")


@label_app.command("review")
def review(
    directory: Path | None = typer.Option(None, "--dir", help=_DIR_HELP),
) -> None:
    """Validate every corpus label loads and every matcher oracle is independent.

    Non-zero exit on any failure: a label that does not parse/validate
    (``EvalSpecError``) or a matcher-oracle label whose labeller is the rule's
    author (``CircularOracleError``).
    """
    try:
        labels = discover_corpus(directory)
    except EvalSpecError as exc:
        typer.echo(f"label review FAILED: {exc}", err=True)
        sys.exit(1)
    failures = [failure for label in labels if (failure := _circular_failure(label)) is not None]
    for failure in failures:
        typer.echo(f"FAIL {failure}", err=True)
    if failures:
        sys.exit(1)
    typer.echo(f"OK — {len(labels)} label(s) load; every matcher oracle independent")


def _circular_failure(label: CorpusLabel) -> str | None:
    # Static validation: a ``both`` label can be graded matcher-only in the
    # no-judge default lane, so review it as the worst case (no judge present) —
    # a same-author ``both`` label that would be circular without a judge fails here.
    try:
        assert_independent_oracle(label, judge_present=False)
    except CircularOracleError as exc:
        return str(exc)
    return None


def _refuse_on_redaction_hit(body: str) -> None:
    redact_terms, overlay_block_patterns = _overlay_privacy_rules()
    verdict = scan_for_publication(
        text=body,
        target_repo=_PUBLIC_TARGET,
        public_repos=[_PUBLIC_TARGET],
        redact_terms=redact_terms,
        block_patterns=[*_PUBLIC_CORPUS_BLOCK_PATTERNS, *overlay_block_patterns],
    )
    if not verdict.refused:
        return
    names = ", ".join(sorted({match.pattern_name for match in verdict.matches}))
    typer.echo(
        f"REFUSED — the capture trips the publication privacy scanner "
        f"({len(verdict.matches)} match(es): {names}); redact it before adding to the public corpus",
        err=True,
    )
    sys.exit(1)


def _overlay_privacy_rules() -> tuple[list[str], list[str]]:
    """The active overlay's ``(privacy_redact_terms, privacy_block_patterns)``.

    Resolving the overlay is best-effort: when no single overlay is resolvable
    (none installed, or several with no ``T3_OVERLAY_NAME`` to disambiguate), the
    public-corpus default block set still applies, so the guard never silently
    weakens — it only loses the overlay's own customer-domain terms.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred Django import.

    try:
        config = get_overlay().config
    except (ImproperlyConfigured, ImportError):
        return [], []
    return list(config.privacy_redact_terms), list(config.privacy_block_patterns)


def _default_entry_id(session_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", session_id.lower()).strip("_")


def _label_template(entry_id: str, session_id: str, record: "SessionAuditRecord") -> str:
    today = datetime.now(tz=UTC).date().isoformat()
    return (
        f"- entry_id: {entry_id}\n"
        '  labelled_by: ""  # REQUIRED — the human labeller (never the rule author)\n'
        f'  labelled_at: "{today}"\n'
        '  expected_behavior: ""  # REQUIRED — what the session should have done\n'
        f"  outcome_axis: {json.dumps(record.outcome_axis)}\n"
        f"  expected_outcome: {json.dumps(record.expected_outcome)}\n"
        "  confidence: medium\n"
        "  oracle: matcher\n"
        '  rule_author: ""  # the identity that authored the rule under test\n'
        f"  source_session_id: {json.dumps(session_id)}\n"
        "  expect: []  # REQUIRED for a matcher oracle — at least one matcher\n"
    )


def _build_nominated_table(records: "list[SessionAuditRecord]") -> Table:
    table = Table(title="Nominated for labelling", show_lines=False)
    table.add_column("Session", style="bold")
    table.add_column("Axis")
    table.add_column("Predicted")
    table.add_column("Gate slugs")
    for record in records:
        table.add_row(
            record.session_id,
            record.outcome_axis,
            record.predicted_outcome,
            ", ".join(record.gate_failure_slugs),
        )
    return table
