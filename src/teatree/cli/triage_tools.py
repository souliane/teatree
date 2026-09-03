"""Issue-triage tool commands — label/dedup/stale scanners.

Split out of ``cli/tools.py`` (which had outgrown the per-file
module-health function cap): every command here is a thin front-end over
``teatree.triage``. Commands register onto the shared ``tool_app`` so
the user-facing CLI surface (``t3 tool label-issues`` etc.) is
byte-for-byte unchanged — this is purely a source-file split.

Importing this module has the side effect of registering the commands;
``cli/__init__`` imports it after ``tool_app`` is constructed.
"""

import typer

from teatree.triage import HIGH_CONFIDENCE, DuplicateFinder, ForgeEnumerationError, LabelSuggester, TriageScanner


def _unknown(exc: ForgeEnumerationError) -> typer.Exit:
    """Report an enumeration that did not run as UNKNOWN, and exit non-zero.

    A verdict is a claim about a set that was enumerated. When the enumeration
    failed the honest answer is UNKNOWN — never a clean "none found" the scan never
    established, which is what let this command report a clear backlog while both
    its ``gh`` calls had failed unauthenticated (#4135).
    """
    typer.echo(f"UNKNOWN  the scan did not run: {exc}. Nothing was examined — this is not a clean result.", err=True)
    return typer.Exit(code=1)


def label_issues(
    repo: str = typer.Argument(..., help="Repository in owner/name form (e.g. souliane/teatree)"),
    *,
    apply: bool = typer.Option(False, "--apply", help="Apply labels via `gh issue edit` (default: print only)."),
) -> None:
    """Suggest labels for unlabeled open issues by keyword-matching title and body."""
    suggester = LabelSuggester(repo)
    try:
        suggestions = suggester.collect_suggestions()
    except ForgeEnumerationError as exc:
        raise _unknown(exc) from exc
    if not suggestions:
        typer.echo("No labelable issues found.")
        return

    for s in suggestions:
        typer.echo(f"#{s.number} {s.title}  -> {', '.join(s.labels)}")

    if not apply:
        typer.echo(f"\n{len(suggestions)} issue(s) to label. Re-run with --apply to apply.")
        return
    failed = suggester.apply(suggestions)
    typer.echo(f"Applied labels to {len(suggestions) - len(failed)} issue(s).")
    if failed:
        typer.echo(f"FAILED to label {len(failed)} issue(s): {', '.join(f'#{n}' for n in failed)}")
        raise typer.Exit(code=1)


def find_duplicates(
    repo: str = typer.Argument(..., help="Repository in owner/name form (e.g. souliane/teatree)"),
    *,
    threshold: float = typer.Option(
        0.75,
        "--threshold",
        min=0.0,
        max=1.0,
        help="Similarity ratio required to flag a pair (0.0-1.0).",
    ),
) -> None:
    """Flag pairs of open issues with near-identical titles."""
    finder = DuplicateFinder(repo, threshold=threshold)
    try:
        matches = finder.find()
    except ForgeEnumerationError as exc:
        raise _unknown(exc) from exc
    if not matches:
        typer.echo("No potential duplicates found.")
        return

    for match in matches:
        typer.echo(
            f"  {match.score:.2f}  #{match.a_number} {match.a_title}\n         #{match.b_number} {match.b_title}"
        )
    typer.echo(f"\n{len(matches)} potential duplicate pair(s).")


def triage_issues(
    repo: str = typer.Argument(..., help="Repository in owner/name form (e.g. souliane/teatree)"),
    *,
    stale_days: int = typer.Option(30, "--stale-days", help="Inactivity threshold for stale-issue detection."),
    close_resolved: bool = typer.Option(
        False, "--close-resolved", help="Close resolved-but-open issues (with comment linking the merged PR)."
    ),
) -> None:
    """Scan for resolved-but-open and stale issues."""
    scanner = TriageScanner(repo)
    failed = _report_resolved(scanner, close_resolved=close_resolved)
    _report_stale(scanner, stale_days=stale_days)
    if failed:
        raise typer.Exit(code=1)


def _report_resolved(scanner: TriageScanner, *, close_resolved: bool) -> list[int]:
    """Print the resolved-but-open section; return the issues that could not be closed."""
    try:
        resolved = scanner.find_resolved()
    except ForgeEnumerationError as exc:
        raise _unknown(exc) from exc
    if not resolved:
        typer.echo("No resolved-but-open issues found.")
        return []

    typer.echo(f"\n{'=' * 60}\n Resolved-but-open ({len(resolved)} issue(s))\n{'=' * 60}")
    for r in resolved:
        typer.echo(f"  #{r.issue_number}  {r.issue_title}")
        typer.echo(f"    ↳ merged PR #{r.pr_number}: {r.pr_title}  [{r.confidence}]")

    closable = [r for r in resolved if r.confidence == HIGH_CONFIDENCE]
    failed: list[int] = []
    if close_resolved:
        failed = scanner.close_resolved(resolved)
        typer.echo(f"Closed {len(closable) - len(failed)} resolved issue(s).")
        if failed:
            typer.echo(f"FAILED to close {len(failed)} issue(s): {', '.join(f'#{n}' for n in failed)}")
    else:
        typer.echo("\nRe-run with --close-resolved to close these issues.")

    if len(closable) != len(resolved):
        skipped = [r.issue_number for r in resolved if r.confidence != HIGH_CONFIDENCE]
        typer.echo(f"Left open for review — a loose `#N` mention is not a fix: {', '.join(f'#{n}' for n in skipped)}")
    return failed


def _report_stale(scanner: TriageScanner, *, stale_days: int) -> None:
    """Print the stale-issue section."""
    try:
        stale = scanner.find_stale(days=stale_days)
    except ForgeEnumerationError as exc:
        raise _unknown(exc) from exc
    if not stale:
        typer.echo(f"No stale issues (unlabeled, inactive >{stale_days}d).")
        return
    typer.echo(f"\n{'=' * 60}\n Stale issues — unlabeled, inactive >{stale_days}d ({len(stale)})\n{'=' * 60}")
    for s in stale:
        typer.echo(f"  #{s.issue_number}  {s.issue_title}  ({s.days_inactive}d inactive)")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("label-issues")(label_issues)
    app.command("find-duplicates")(find_duplicates)
    app.command("triage-issues")(triage_issues)
