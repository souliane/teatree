"""CI CLI commands — pipeline helpers."""

import os
import subprocess  # noqa: S404

import typer

from teetree.backends.protocols import CIService

ci_app = typer.Typer(no_args_is_help=True, help="CI pipeline helpers.")


class CICommands:
    """CI pipeline operations — cancel, fetch errors, trigger, quality check."""

    @staticmethod
    def get_ci_service() -> CIService | None:
        """Get CI service — tries Django settings first, falls back to env vars."""
        try:
            from teetree.backends.loader import get_ci_service  # noqa: PLC0415

            return get_ci_service()
        except Exception:  # noqa: BLE001, S110 — fallback to env-based config
            pass

        token = os.environ.get("TEATREE_GITLAB_TOKEN", os.environ.get("GITLAB_TOKEN", ""))
        if token:
            from teetree.backends.gitlab_ci import GitLabCIService  # noqa: PLC0415
            from teetree.utils.gitlab_api import GitLabAPI  # noqa: PLC0415

            base_url = os.environ.get("TEATREE_GITLAB_URL", "https://gitlab.com/api/v4")
            return GitLabCIService(client=GitLabAPI(token=token, base_url=base_url))
        return None

    @staticmethod
    def get_ci_project() -> str:
        """Get the CI project path — from overlay or git remote."""
        try:
            import django  # noqa: PLC0415

            django.setup()
            from teetree.core.overlay_loader import get_overlay  # noqa: PLC0415

            project = get_overlay().get_ci_project_path()
            if project:
                return project
        except Exception:  # noqa: BLE001, S110 — Django may not be configured
            pass

        from teetree.utils.gitlab_api import GitLabAPI  # noqa: PLC0415

        project_info = GitLabAPI().resolve_project_from_remote()
        return project_info.path_with_namespace if project_info else ""

    @staticmethod
    def current_git_branch() -> str:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""


def _require_ci() -> tuple[CIService, str]:
    """Resolve CI service and project."""
    ci = CICommands.get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set TEATREE_GITLAB_TOKEN).")
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
    from teetree.utils.git import current_branch  # noqa: PLC0415
    from teetree.utils.git import run as git_run  # noqa: PLC0415

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
