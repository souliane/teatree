"""``t3 tool rule-clauses`` — refuse a RULES.md diff that drops a clause no commit trailer names.

The analysis lives in :mod:`teatree.quality.rule_clauses`; this module is the thin CLI surface.
Exit codes: ``0`` when the file is untouched or every old clause is kept or named, ``1`` when an
unnamed clause is missing, ``2`` when git cannot say.
"""

import typer

from teatree.quality.rule_clauses import RULES_PATH, rules_diff


def rule_clauses_cmd(
    repo: str = typer.Option(".", "--repo", help="Repository to read."),
    base: str = typer.Option("origin/main", "--base", help="Ref the branch is compared against."),
    head: str = typer.Option("HEAD", "--head", help="Ref carrying the edited rules."),
) -> None:
    """Refuse a RULES.md diff that drops, rewords or moves a clause no ``Rule-clause-removed:`` trailer names."""
    try:
        diff = rules_diff(repo, base=base, head=head)
    except RuntimeError as exc:
        typer.echo(f"rule-clauses could not read the diff: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not diff.touched:
        typer.echo(f"PASS — {RULES_PATH} is untouched.")
        return
    if refusal := diff.refusal():
        typer.echo(refusal, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"PASS — every clause of {RULES_PATH} is still present or its removal is named.")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("rule-clauses")(rule_clauses_cmd)
