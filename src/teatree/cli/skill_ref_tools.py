"""Skill-reference validation command — dangling skill-name detection.

Split out of ``cli/tools.py`` (which had reached the per-file module-health
function cap): the command registers onto the shared ``tool_app`` so the
user-facing surface (``t3 tool validate-skill-refs``) is unchanged.

Importing this module has the side effect of registering the command;
``cli/__init__`` imports it after ``tool_app`` is constructed.
"""

import json
from pathlib import Path

import typer


def validate_skill_refs_cmd(
    *,
    agents_dir: Path | None = typer.Option(
        None,
        "--agents-dir",
        help="Directory of agent *.md files to scan (default: this plugin's agents/).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Assert every skill reference resolves to a real skill in the canonical set.

    Enumerates the canonical skill set from the actual installed/remote skills
    (the same search dirs the skill-loading hook reads — ``~/.claude/skills/*``
    symlinks plus this plugin's ``skills/`` tree), then checks the ``agents/*.md``
    frontmatter ``skills:`` / ``companion_skills:`` lists. A dangling name (e.g. the
    real ``ac-reviewing-skills`` -> ``ac-reviewing-codebase`` incident) exits non-zero
    with file:line + the bad name + nearest matches.
    """
    from teatree.skill_support.ref_validator import validate_skill_refs  # noqa: PLC0415 — deferred: lazy CLI import

    findings = validate_skill_refs(agents_dir=agents_dir)
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {"path": str(f.path), "line": f.line, "name": f.name, "site": f.site, "suggestions": f.suggestions}
                    for f in findings
                ]
            )
        )
    else:
        for finding in findings:
            typer.echo(finding.render(), err=True)
    if findings:
        raise typer.Exit(code=1)
    if not json_output:
        typer.echo("PASS — all skill references resolve.")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("validate-skill-refs")(validate_skill_refs_cmd)
