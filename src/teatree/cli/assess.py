"""Assess CLI — run deterministic codebase metrics and track history."""

import json
import subprocess  # noqa: S404
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

assess_app = typer.Typer(no_args_is_help=True, help="Codebase health assessment.")
console = Console()

ASSESSMENTS_DIR = ".t3/assessments"


def _find_skill_cli() -> Path | None:
    """Find ac-reviewing-codebase's cli.py in known skill locations."""
    candidates = [
        Path.home() / ".claude" / "skills" / "ac-reviewing-codebase" / "scripts" / "cli.py",
        Path.home() / ".agents" / "skills" / "ac-reviewing-codebase" / "scripts" / "cli.py",
        Path.home() / ".cursor" / "skills" / "ac-reviewing-codebase" / "scripts" / "cli.py",
    ]
    return next((p for p in candidates if p.exists()), None)


@assess_app.command("run")
def run_assessment(
    root: Path = typer.Option(None, help="Repository root to assess"),
    *,
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
    save: bool = typer.Option(True, "--save/--no-save", help="Save results to .t3/assessments/"),
) -> None:
    """Run deterministic codebase metrics on a repository."""
    if root is None:
        root = Path.cwd()
    cli_path = _find_skill_cli()
    if not cli_path:
        typer.echo("ac-reviewing-codebase skill not found. Install: apm install souliane/skills/ac-reviewing-codebase")
        raise typer.Exit(1)

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(cli_path), "assess", "--root", str(root), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        typer.echo(f"Assessment failed: {result.stderr.strip()}")
        raise typer.Exit(1)

    try:
        metrics = json.loads(result.stdout)
    except json.JSONDecodeError:
        typer.echo(f"Invalid JSON from skill CLI: {result.stdout[:200]}")
        raise typer.Exit(1) from None

    if save:
        _save_assessment(root, metrics)

    if output_json:
        typer.echo(json.dumps(metrics, indent=2))
    else:
        _print_summary(metrics)


@assess_app.command("history")
def show_history(
    root: Path = typer.Option(None, help="Repository root"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of recent assessments to show"),
) -> None:
    """Show assessment history for a repository."""
    if root is None:
        root = Path.cwd()
    assessments_dir = root / ASSESSMENTS_DIR
    if not assessments_dir.exists():
        typer.echo("No assessments found. Run: t3 assess run")
        raise typer.Exit(1)

    files = sorted(assessments_dir.glob("*.json"), reverse=True)[:limit]
    if not files:
        typer.echo("No assessment files found.")
        raise typer.Exit(1)

    table = Table(title="Assessment History", show_lines=False)
    table.add_column("Date", style="bold")
    table.add_column("Lint", justify="right")
    table.add_column("TODOs", justify="right")
    table.add_column("Complex", justify="right")
    table.add_column("Coverage", justify="right")
    table.add_column("Outdated", justify="right")
    table.add_column("Suppressions", justify="right")

    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        metrics = data.get("metrics", data)
        lint = metrics.get("lint", {})
        todos = metrics.get("todos", {})
        cx = metrics.get("complexity", {})
        cov = metrics.get("coverage", {})
        deps = metrics.get("dependencies", {})
        supps = metrics.get("suppressions", {})

        table.add_row(
            f.stem,
            str(lint.get("total", "?")),
            str(todos.get("total", "?")),
            str(cx.get("violations", "?")),
            f"{cov['percent']:.0f}%" if cov.get("available") else "-",
            str(deps.get("outdated_count", "-")) if deps.get("available") else "-",
            str(sum(supps.values())) if supps else "0",
        )

    console.print(table)


def _save_assessment(root: Path, metrics: dict) -> None:
    """Save assessment to .t3/assessments/YYYY-MM-DD.json."""
    assessments_dir = root / ASSESSMENTS_DIR
    assessments_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    output = {
        "date": date_str,
        "repo": root.name,
        "metrics": metrics,
    }

    out_path = assessments_dir / f"{date_str}.json"
    out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Saved: {out_path}")


def _print_summary(metrics: dict) -> None:
    """Print a human-readable assessment summary."""
    console.print("[bold]Codebase Assessment[/bold]")
    console.print()

    lint = metrics.get("lint", {})
    if "error" not in lint:
        color = "green" if lint.get("total", 0) == 0 else "yellow"
        console.print(f"  Lint violations: [{color}]{lint.get('total', '?')}[/{color}]")

    todos = metrics.get("todos", {})
    console.print(f"  TODOs/FIXMEs: {todos.get('total', '?')}")

    cx = metrics.get("complexity", {})
    if "error" not in cx:
        console.print(f"  Complex functions: {cx.get('violations', '?')}")

    cov = metrics.get("coverage", {})
    if cov.get("available"):
        pct = cov["percent"]
        color = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"  # noqa: PLR2004
        console.print(f"  Test coverage: [{color}]{pct:.1f}%[/{color}]")

    deps = metrics.get("dependencies", {})
    if deps.get("available"):
        n = deps["outdated_count"]
        color = "green" if n == 0 else "yellow"
        console.print(f"  Outdated deps: [{color}]{n}[/{color}]")

    supps = metrics.get("suppressions", {})
    total_supps = sum(supps.values()) if supps else 0
    color = "green" if total_supps == 0 else "yellow"
    console.print(f"  Lint suppressions: [{color}]{total_supps}[/{color}]")
