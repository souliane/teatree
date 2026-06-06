"""CI CLI commands — pipeline helpers."""

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from teatree.core.backend_protocols import CIService
from teatree.utils.coverage_floor import measure_coverage
from teatree.utils.django_bootstrap import ensure_django

ci_app = typer.Typer(no_args_is_help=True, help="CI pipeline helpers.")
_console = Console()


class CICommands:
    """CI pipeline operations — cancel, fetch errors, trigger, quality check."""

    @staticmethod
    def get_ci_service() -> CIService | None:
        """Get CI service — tries overlay first, falls back to env vars."""
        try:
            from teatree.core.backend_factory import ci_service_from_overlay  # noqa: PLC0415

            result = ci_service_from_overlay()
            if result is not None:
                return result
        except Exception:  # noqa: BLE001, S110 — fallback to env-based config
            pass

        token = os.environ.get("GITLAB_TOKEN", "")
        if token:
            from teatree.backends.loader import get_ci_service  # noqa: PLC0415

            base_url = os.environ.get("GITLAB_URL", "https://gitlab.com/api/v4")
            return get_ci_service(gitlab_token=token, gitlab_url=base_url)
        return None

    @staticmethod
    def get_ci_project() -> str:
        """Get the CI project path — from overlay or git remote."""
        try:
            ensure_django()
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

            project = get_overlay().metadata.get_ci_project_path()
            if project:
                return project
        except Exception:  # noqa: BLE001, S110 — Django may not be configured
            pass

        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415

        project_info = GitLabAPI().resolve_project_from_remote()
        return project_info.path_with_namespace if project_info else ""

    @staticmethod
    def current_git_branch() -> str:
        from teatree.utils.git import current_branch  # noqa: PLC0415

        return current_branch()


def _require_ci() -> tuple[CIService, str]:
    """Resolve CI service and project."""
    ci = CICommands.get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set GITLAB_TOKEN or configure overlay).")
        raise typer.Exit(code=1)
    project = CICommands.get_ci_project()
    return ci, project


@ci_app.command()
def cancel(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Cancel stale CI pipelines for a branch."""
    ci, project = _require_ci()
    ref = branch or CICommands.current_git_branch()
    if not ref:
        typer.echo("Could not detect branch. Pass one explicitly.")
        raise typer.Exit(code=1)

    cancelled = ci.cancel_pipelines(project=project, ref=ref)
    if cancelled:
        typer.echo(f"Cancelled {len(cancelled)} pipeline(s): {cancelled}")
    else:
        typer.echo("No running/pending pipelines found.")


@ci_app.command()
def divergence() -> None:
    """Check fork divergence from upstream."""
    from teatree.utils.git import current_branch  # noqa: PLC0415
    from teatree.utils.git import run as git_run  # noqa: PLC0415

    try:
        git_run(repo=".", args=["fetch", "upstream"])
    except Exception:  # noqa: BLE001
        typer.echo("No upstream remote configured. Add one: git remote add upstream <url>")
        raise typer.Exit(code=1) from None

    ref = current_branch()
    ahead = git_run(repo=".", args=["rev-list", "--count", f"upstream/{ref}..HEAD"]).strip()
    behind = git_run(repo=".", args=["rev-list", "--count", f"HEAD..upstream/{ref}"]).strip()
    typer.echo(f"Branch {ref}: {ahead} ahead, {behind} behind upstream")


@ci_app.command(name="fetch-errors")
def fetch_errors(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Fetch error logs from the latest CI pipeline."""
    ci, project = _require_ci()
    ref = branch or CICommands.current_git_branch()
    errors = ci.fetch_pipeline_errors(project=project, ref=ref)
    if errors:
        for error in errors:
            typer.echo(error)
            typer.echo("---")
    else:
        typer.echo("No errors found in the latest pipeline.")


@ci_app.command(name="fetch-failed-tests")
def fetch_failed_tests(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Extract failed test IDs from the latest CI pipeline."""
    ci, project = _require_ci()
    ref = branch or CICommands.current_git_branch()
    failed = ci.fetch_failed_tests(project=project, ref=ref)
    if failed:
        typer.echo(f"Failed tests ({len(failed)}):")
        for test_id in failed:
            typer.echo(f"  {test_id}")
    else:
        typer.echo("No failed tests found.")


@ci_app.command(name="trigger-e2e")
def trigger_e2e(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Trigger E2E tests on CI."""
    ci, project = _require_ci()
    ref = branch or CICommands.current_git_branch()
    result = ci.trigger_pipeline(project=project, ref=ref, variables={"E2E": "true"})
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(code=1)
    typer.echo(f"Pipeline triggered: {result.get('web_url', result.get('id', 'unknown'))}")


@ci_app.command(name="coverage")
def coverage(
    *,
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
    coverage_file: Path = typer.Option(
        Path(".coverage"),
        "--coverage-file",
        help="Path to .coverage data file",
    ),
    pyproject: Path = typer.Option(
        Path("pyproject.toml"),
        "--pyproject",
        help="Path to pyproject.toml",
    ),
) -> None:
    """Print current coverage and the configured floor; non-zero on failure.

    Reads ``[tool.coverage.report] fail_under`` and ``[tool.teatree.coverage]
    per_module_floors`` from ``pyproject.toml``. Loads ``.coverage`` for the
    measured percentages. Exits 1 if any floor is missed.
    """
    report = measure_coverage(pyproject_path=pyproject, coverage_data_file=coverage_file)

    if output_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
        if not report.passes():
            raise typer.Exit(code=1)
        return

    _console.print(f"[bold]Coverage floor:[/bold] {report.overall_floor}%")
    if report.overall_percent is None:
        _console.print(
            f"[yellow]Coverage not measured (no .coverage at {coverage_file}). Run `uv run pytest` first.[/yellow]",
        )
        raise typer.Exit(code=1)

    color = "green" if report.overall_percent >= report.overall_floor else "red"
    _console.print(f"[bold]Overall:[/bold] [{color}]{report.overall_percent:.1f}%[/{color}]")

    if report.module_results:
        table = Table(title="Per-module floors", show_lines=False)
        table.add_column("Module")
        table.add_column("Floor", justify="right")
        table.add_column("Actual", justify="right")
        table.add_column("Status", justify="right")
        for m in report.module_results:
            actual = f"{m.percent:.1f}%" if m.percent is not None else "—"
            ok = m.passes()
            status_color = "green" if ok else "red"
            status = "OK" if ok else "FAIL"
            table.add_row(m.path, f"{m.floor}%", actual, f"[{status_color}]{status}[/{status_color}]")
        _console.print(table)

    if not report.passes():
        raise typer.Exit(code=1)


@ci_app.command(name="quality-check")
def quality_check(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Run quality analysis (fetch test report from latest pipeline)."""
    ci, project = _require_ci()
    ref = branch or CICommands.current_git_branch()
    report = ci.quality_check(project=project, ref=ref)
    if "error" in report:
        typer.echo(f"Error: {report['error']}")
        raise typer.Exit(code=1)
    typer.echo(f"Pipeline {report.get('pipeline_id')}: {report.get('status')}")
    typer.echo(f"  Total: {report.get('total_count', 0)}")
    typer.echo(f"  Passed: {report.get('success_count', 0)}")
    typer.echo(f"  Failed: {report.get('failed_count', 0)}")
