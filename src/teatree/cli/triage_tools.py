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

from teatree.triage import DuplicateFinder, LabelSuggester


def label_issues(
    repo: str = typer.Argument(..., help="Repository in owner/name form (e.g. souliane/teatree)"),
    *,
    apply: bool = typer.Option(False, "--apply", help="Apply labels via `gh issue edit` (default: print only)."),
) -> None:
    """Suggest labels for unlabeled open issues by keyword-matching title and body."""
    suggester = LabelSuggester(repo)
    suggestions = suggester.collect_suggestions()
    if not suggestions:
        typer.echo("No labelable issues found.")
        return

    for s in suggestions:
        typer.echo(f"#{s.number} {s.title}  -> {', '.join(s.labels)}")

    if apply:
        suggester.apply(suggestions)
        typer.echo(f"Applied labels to {len(suggestions)} issue(s).")
    else:
        typer.echo(f"\n{len(suggestions)} issue(s) to label. Re-run with --apply to apply.")


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
    matches = finder.find()
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
    from teatree.triage import TriageScanner  # noqa: PLC0415 — deferred: keeps CLI startup light

    scanner = TriageScanner(repo)

    resolved = scanner.find_resolved()
    if resolved:
        typer.echo(f"\n{'=' * 60}\n Resolved-but-open ({len(resolved)} issue(s))\n{'=' * 60}")
        for r in resolved:
            typer.echo(f"  #{r.issue_number}  {r.issue_title}")
            typer.echo(f"    ↳ merged PR #{r.pr_number}: {r.pr_title}  [{r.confidence}]")
        if close_resolved:
            scanner.close_resolved(resolved)
            typer.echo(f"Closed {len(resolved)} resolved issue(s).")
        else:
            typer.echo("\nRe-run with --close-resolved to close these issues.")
    else:
        typer.echo("No resolved-but-open issues found.")

    stale = scanner.find_stale(days=stale_days)
    if stale:
        typer.echo(f"\n{'=' * 60}\n Stale issues — unlabeled, inactive >{stale_days}d ({len(stale)})\n{'=' * 60}")
        for s in stale:
            typer.echo(f"  #{s.issue_number}  {s.issue_title}  ({s.days_inactive}d inactive)")
    else:
        typer.echo(f"No stale issues (unlabeled, inactive >{stale_days}d).")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("label-issues")(label_issues)
    app.command("find-duplicates")(find_duplicates)
    app.command("triage-issues")(triage_issues)
