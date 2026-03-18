"""Discover open MRs, validate metadata, and prepare review request summary.

Used by: t3-review-request (§§1-6).
Reuses lib/gitlab.py (shared with t3-followup) for MR discovery.
The agent handles: chat search, user confirmation, and actual posting.
"""

import json
import re
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.gitlab import (
    current_user,
    discover_mrs,
    get_mr_approvals,
    get_mr_pipeline,
)

app = typer.Typer(add_completion=False)
console = Console(stderr=True)

_CONVENTIONAL_RE = re.compile(
    r"^(?:feat|fix|improvement|refactor|chore|docs|test|style|perf|ci|build|revert)"
    r"(?:\([^)]+\))?"
    r":\s.+"
)


def _validate_mr_title(title: str) -> list[str]:
    issues: list[str] = []
    if not _CONVENTIONAL_RE.match(title):
        issues.append("Title doesn't match conventional commit format")
    return issues


def _validate_mr_description(description: str) -> list[str]:
    if not description:
        return ["Empty description"]
    first_line = description.split("\n", maxsplit=1)[0].strip()
    if not first_line:
        return ["First line of description is empty"]
    return []


def _ci_display(status: str | None) -> str:
    return {"success": "green", "failed": "failed"}.get(
        status or "", "running" if status in {"running", "pending"} else "unknown"
    )


def _discover_and_validate(
    repos: list[str],
    username: str,
    *,
    verbose: bool = False,
) -> list[dict]:
    """Discover open non-draft MRs and validate each."""
    raw_mrs = discover_mrs(repos, username, include_draft=False, verbose=verbose)
    results: list[dict] = []

    for mr in raw_mrs:
        iid = mr["iid"]
        project_id = mr["_project_id"]
        pipeline = get_mr_pipeline(project_id, iid)
        approvals = get_mr_approvals(project_id, iid)
        validation_issues = _validate_mr_title(mr.get("title", "")) + _validate_mr_description(
            mr.get("description", "")
        )
        ci = _ci_display(pipeline.get("status"))

        results.append(
            {
                "repo": mr["_repo_short"],
                "project_id": project_id,
                "iid": iid,
                "title": mr.get("title", ""),
                "url": mr.get("web_url", ""),
                "branch": mr.get("source_branch", ""),
                "updated_at": mr.get("updated_at", ""),
                "ci_status": ci,
                "pipeline_url": pipeline.get("url"),
                "validation_issues": validation_issues,
                "valid": len(validation_issues) == 0,
                "approvals": approvals.get("count", 0),
                "approvals_required": approvals.get("required", 0),
            }
        )

    results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return results


@app.command()
def main(
    repos: str = typer.Option("", envvar="T3_FOLLOWUP_REPOS", help="Comma-separated repo paths"),
    *,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Discover open MRs, validate metadata, and prepare review request summary."""
    repo_list = [r.strip() for r in repos.split(",") if r.strip()]
    if not repo_list:
        console.print("[red]No repos configured. Set T3_FOLLOWUP_REPOS.[/]")
        raise SystemExit(1)

    username = current_user()
    if not username:
        console.print("[red]Could not detect GitLab username[/]")
        raise SystemExit(1)

    if verbose:
        console.print(f"User: {username}")

    results = _discover_and_validate(repo_list, username, verbose=verbose)

    if json_output:
        print(json.dumps(results, indent=2))
        return

    if not results:
        console.print("[dim]No open non-draft MRs found[/]")
        return

    table = Table(title="MR Overview")
    table.add_column("MR", style="bold")
    table.add_column("Title")
    table.add_column("CI", justify="center")
    table.add_column("Valid", justify="center")
    table.add_column("Ready", justify="center")

    ready_count = 0
    for mr in results:
        ci_icon = {"green": "[green]✅[/]", "failed": "[red]❌[/]", "running": "[yellow]🔄[/]"}.get(
            mr["ci_status"], "[dim]?[/]"
        )
        valid_icon = "[green]✅[/]" if mr["valid"] else f"[red]❌ {'; '.join(mr['validation_issues'])}[/]"
        is_ready = mr["ci_status"] == "green" and mr["valid"]
        ready_icon = "[green]✅[/]" if is_ready else "[dim]⏳[/]"
        if is_ready:
            ready_count += 1
        table.add_row(f"{mr['repo']}!{mr['iid']}", mr["title"][:60], ci_icon, valid_icon, ready_icon)

    console.print(table)
    console.print(f"\n{ready_count} MR(s) ready for review request")
