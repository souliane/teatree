#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "typer>=0.12",
#   "rich>=13",
# ]
# ///
"""Check fork divergence from upstream before contributing.

Used by: t3-contribute (§5a Divergence Analysis).
"""

import json
import sys
from pathlib import Path

import typer
from rich.console import Console

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

from lib.git import check as git_check
from lib.git import run as git_run

app = typer.Typer(add_completion=False)
console = Console(stderr=True)


def _analyze(
    repo: str,
    upstream: str,
    branch: str,
    *,
    max_fork_only: int,
    max_upstream_only: int,
) -> dict[str, str | int | bool]:
    if not branch:
        branch = git_run(repo=repo, args=["rev-parse", "--abbrev-ref", "HEAD"])

    if not git_check(repo=repo, args=["remote", "get-url", "upstream"]):
        git_run(repo=repo, args=["remote", "add", "upstream", f"https://github.com/{upstream}.git"])

    git_run(repo=repo, args=["fetch", "upstream"])

    upstream_default = ""
    for line in git_run(repo=repo, args=["remote", "show", "upstream"]).splitlines():
        if "HEAD branch:" in line:
            upstream_default = line.split("HEAD branch:")[-1].strip()
            break
    upstream_default = upstream_default or "main"

    merge_base = git_run(repo=repo, args=["merge-base", f"origin/{branch}", f"upstream/{upstream_default}"])
    merge_base_date = git_run(repo=repo, args=["log", "-1", "--format=%ci", merge_base]) if merge_base else ""

    fork_log = git_run(repo=repo, args=["log", "--oneline", f"upstream/{upstream_default}..origin/{branch}"])
    fork_only = len(fork_log.splitlines()) if fork_log else 0

    upstream_log = git_run(repo=repo, args=["log", "--oneline", f"origin/{branch}..upstream/{upstream_default}"])
    upstream_only = len(upstream_log.splitlines()) if upstream_log else 0

    return {
        "branch": branch,
        "upstream": f"{upstream}/{upstream_default}",
        "merge_base": merge_base[:12] if merge_base else "unknown",
        "merge_base_date": merge_base_date,
        "fork_only": fork_only,
        "upstream_only": upstream_only,
        "blocked": fork_only > max_fork_only or upstream_only > max_upstream_only,
    }


@app.command()
def main(  # noqa: PLR0913
    repo: str = typer.Argument(help="Path to the git repository to check"),
    upstream: str = typer.Option(help="Upstream GitHub repo (e.g. owner/repo)"),
    branch: str = typer.Option("", help="Branch to check (default: current)"),
    max_fork_only: int = typer.Option(50, help="Max fork-only commits before blocking"),
    max_upstream_only: int = typer.Option(20, help="Max upstream-only commits before blocking"),
    *,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Analyze fork divergence from upstream."""
    data = _analyze(repo, upstream, branch, max_fork_only=max_fork_only, max_upstream_only=max_upstream_only)

    if json_output:
        print(json.dumps(data, indent=2))
    else:
        status = "[red]BLOCKED[/]" if data["blocked"] else "[green]OK[/]"
        console.print(f"\n  Fork divergence: {status}")
        console.print(f"  Branch: origin/{data['branch']} vs upstream/{data['upstream']}")
        console.print(f"  Common base: {data['merge_base']} ({data['merge_base_date']})")
        console.print(f"  Fork-only commits: {data['fork_only']} (max {max_fork_only})")
        console.print(f"  Upstream-only commits: {data['upstream_only']} (max {max_upstream_only})")
        console.print()

    if data["blocked"]:
        raise SystemExit(1)


if __name__ == "__main__":
    app()
