#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "typer>=0.12",
#   "rich>=13",
# ]
# ///
"""Run E2E tests with automatic environment setup.

Eliminates all manual steps: ensures worktree is ready, servers are running,
translations loaded, then runs Playwright headless. The agent calls one command.

Used by: t3 test e2e
"""

import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

from lib.env import detect_ticket_dir, resolve_context
from lib.lifecycle import WorktreeLifecycle

app = typer.Typer(add_completion=False)
console = Console(stderr=True)


def _abort_missing(name: str) -> str:
    console.print(f"[bold red]Missing required config: {name}[/]")
    console.print("Set it via --variant or in .env.worktree (variant=…)")
    raise SystemExit(1)


def _ensure_ready(lc: WorktreeLifecycle, variant: str) -> None:
    """Advance lifecycle to 'ready' state, provisioning and starting as needed."""
    if lc.state == "created":
        console.print("[bold]Provisioning worktree...[/]")
        ctx = resolve_context()
        lc.provision(wt_dir=ctx.wt_dir, main_repo=ctx.main_repo, variant=variant)
        console.print(f"  State: {lc.state}")

    if lc.state == "provisioned":
        console.print("[bold]Starting services...[/]")
        lc.start_services()
        console.print(f"  State: {lc.state}")

    if lc.state == "services_up":
        console.print("[bold]Verifying services...[/]")
        lc.verify()
        console.print(f"  State: {lc.state}")

    if lc.state != "ready":
        console.print(f"[red]Expected state 'ready', got '{lc.state}'[/]")
        raise SystemExit(1)


def _verify_services(ports: dict) -> bool:
    """Quick HTTP check that backend and frontend respond."""
    ok = True
    for name, port in [("backend", ports.get("backend")), ("frontend", ports.get("frontend"))]:
        if not port:
            continue
        result = subprocess.run(
            ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", f"http://localhost:{port}/"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        code = result.stdout.strip()
        if code and code[0] in {"2", "3"}:
            console.print(f"  {name}: [green]{code}[/] on :{port}")
        else:
            console.print(f"  {name}: [red]{code or 'unreachable'}[/] on :{port}")
            ok = False
    return ok


def _find_test_dir() -> Path | None:
    """Find the E2E test directory from T3_PRIVATE_TESTS or common locations."""
    private = os.environ.get("T3_PRIVATE_TESTS", "")
    if private and Path(private).is_dir():
        return Path(private)
    return None


def _run_playwright(  # noqa: PLR0913
    test_dir: Path,
    ports: dict,
    *,
    spec: str,
    variant: str,
    app_name: str,
    headed: bool,
) -> subprocess.CompletedProcess:
    """Run Playwright with correct env vars derived from worktree state."""
    env = {
        **os.environ,
        "CI": "0" if headed else "1",
        "BASE_URL": f"http://localhost:{ports.get('frontend', 4200)}",
        "CUSTOMER": variant,
        "APP": app_name,
    }

    cmd = ["npx", "playwright", "test"]
    if spec:
        cmd.append(spec)
    if headed:
        cmd.append("--headed")

    console.print(f"\n[bold]Running Playwright[/] in {test_dir}")
    console.print(f"  BASE_URL={env['BASE_URL']}")
    console.print(f"  CUSTOMER={variant}  APP={app_name}")
    console.print(f"  Spec: {spec or 'all'}")
    console.print(f"  Mode: {'headed' if headed else 'headless'}")
    console.print()

    return subprocess.run(cmd, cwd=str(test_dir), env=env, check=False)


def _ensure_services_or_fail(lc: WorktreeLifecycle, ports: dict, *, skip_setup: bool) -> None:
    """Verify services respond, restarting if allowed."""
    console.print("[bold]Checking services...[/]")
    if _verify_services(ports):
        return
    if skip_setup:
        console.print("[red]Services not responding. Remove --skip-setup to auto-start.[/]")
        raise SystemExit(1)
    console.print("[yellow]Services not responding — restarting...[/]")
    lc.state = "provisioned"
    lc.save()
    lc.start_services()
    lc.verify()
    if not _verify_services(ports):
        console.print("[red]Services still not responding after restart. Check logs.[/]")
        raise SystemExit(1)


def _report_artifacts(test_dir: Path) -> None:
    artifacts_dir = test_dir / "test-results"
    if not artifacts_dir.is_dir():
        return
    videos = list(artifacts_dir.rglob("*.webm"))
    screenshots = list(artifacts_dir.rglob("*.png"))
    if videos or screenshots:
        console.print(f"\n[bold]Artifacts:[/] {len(videos)} videos, {len(screenshots)} screenshots in {artifacts_dir}")


@app.command()
def main(
    spec: str = typer.Argument("", help="Test file or pattern (e.g. 'brokerage/loans')"),
    variant: str = typer.Option("", help="Customer variant (default: from .env.worktree)"),
    app_name: str = typer.Option("brokerage", "--app", help="App to test: brokerage, self-service"),
    *,
    headed: bool = typer.Option(False, help="Run with visible browser"),
    skip_setup: bool = typer.Option(False, help="Skip lifecycle setup (assume servers running)"),
) -> None:
    """Run E2E tests with automatic environment setup.

    Ensures worktree is provisioned, services are running, then runs Playwright.
    """
    ticket_dir = detect_ticket_dir()
    if not ticket_dir:
        console.print("[red]Not in a ticket directory. Create a worktree first: t3 workspace ticket[/]")
        raise SystemExit(1)

    test_dir = _find_test_dir()
    if not test_dir:
        console.print("[red]No E2E test directory found. Set T3_PRIVATE_TESTS in ~/.teatree[/]")
        raise SystemExit(1)

    lc = WorktreeLifecycle(ticket_dir=ticket_dir)
    if not variant:
        variant = lc.facts.get("variant", "")

    if not skip_setup:
        _ensure_ready(lc, variant)
    elif lc.state != "ready":
        console.print(f"[yellow]Warning: state is '{lc.state}', not 'ready'. Use without --skip-setup to auto-fix.[/]")

    ports = lc.facts.get("ports", {})
    if not ports:
        console.print("[red]No port information in lifecycle state. Run t3 lifecycle setup first.[/]")
        raise SystemExit(1)

    _ensure_services_or_fail(lc, ports, skip_setup=skip_setup)

    result = _run_playwright(
        test_dir,
        ports,
        spec=spec,
        variant=variant or _abort_missing("variant (customer name)"),
        app_name=app_name,
        headed=headed,
    )
    _report_artifacts(test_dir)

    if result.returncode == 0:
        console.print("\n[bold green]E2E tests passed[/]")
    else:
        console.print(f"\n[bold red]E2E tests failed (exit {result.returncode})[/]")
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    app()
