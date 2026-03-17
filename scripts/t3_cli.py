#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""t3 — Unified worktree lifecycle CLI.

Both users and AI agents call this CLI. It wraps the state machine,
extension point registry, and all utility scripts behind a single
entry point with structured output. All commands run in-process.

Commands:
    info         Show extension point registry (layers, handlers, overrides)

Groups:
    lifecycle    State machine (setup, start, teardown)
    workspace    Workspace management (create, finalize, prune)
    run          Dev servers and test runners
    ci           CI pipeline interaction
    db           Database operations
    mr           Merge request and ticket workflow
"""

import json
import sys

import lib.init
import typer

lib.init.init()

from lib.env import detect_ticket_dir, resolve_context
from lib.fsm import generate_mermaid
from lib.lifecycle import WorktreeLifecycle
from lib.registry import call as ep_call

app = typer.Typer(
    name="t3",
    help="Worktree lifecycle manager",
    add_completion=False,
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command()
def info(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show extension point registry — layers, handlers, and overrides."""
    from lib.registry import info as registry_info

    data = registry_info()
    if as_json:
        print(json.dumps(data, indent=2))
        return

    if not data:
        print("No extension points registered.")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title="Extension Point Registry")
    table.add_column("Extension Point", style="cyan")
    table.add_column("Layer", style="green")
    table.add_column("Handler")

    for d in data:
        table.add_row(d["point"], d["active_layer"], d["active_handler"])

    Console().print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_lifecycle() -> WorktreeLifecycle:
    td = detect_ticket_dir()
    if not td:
        print("Error: not in a ticket directory", file=sys.stderr)
        raise typer.Exit(1)
    return WorktreeLifecycle(ticket_dir=td)


# ---------------------------------------------------------------------------
# Group: lifecycle — State machine
# ---------------------------------------------------------------------------

lc_app = typer.Typer(help="Lifecycle state machine (setup, start, teardown)", no_args_is_help=True)
app.add_typer(lc_app, name="lifecycle")


@lc_app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show current worktree state, ports, DB, and available transitions."""
    lc = _get_lifecycle()
    data = lc.status()
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(f"State: {data['state']}")
        if data["facts"].get("ports"):
            ports = data["facts"]["ports"]
            print(f"  Backend:  http://localhost:{ports['backend']}")
            print(f"  Frontend: http://localhost:{ports['frontend']}")
            print(f"  Postgres: localhost:{ports['postgres']}")
        if data["facts"].get("db_name"):
            print(f"  Database: {data['facts']['db_name']}")
        print("Available transitions:")
        for t in data["available_transitions"]:
            conds = f" (requires: {', '.join(t['conditions'])})" if t["conditions"] else ""
            print(f"  t3 lifecycle {t['method'].replace('_', '-')}{conds}")


@lc_app.command()
def diagram() -> None:
    """Print the lifecycle state diagram as Mermaid."""
    print(generate_mermaid(WorktreeLifecycle))


@lc_app.command()
def setup(
    variant: str = typer.Argument("", help="Tenant variant"),
) -> None:
    """Provision worktree: ports, env, symlinks, DB."""
    lc = _get_lifecycle()
    ctx = resolve_context()
    lc.provision(wt_dir=ctx.wt_dir, main_repo=ctx.main_repo, variant=variant)
    print(json.dumps(lc.status(), indent=2, default=str))


@lc_app.command()
def start() -> None:
    """Start dev servers (backend + frontend), then verify."""
    lc = _get_lifecycle()
    lc.start_services()
    lc.verify()
    print(json.dumps(lc.status(), indent=2, default=str))


@lc_app.command()
def clean() -> None:
    """Teardown worktree — stop services, drop DB, clean state."""
    lc = _get_lifecycle()
    lc.teardown()
    print("Worktree cleaned")


# ---------------------------------------------------------------------------
# Group: workspace — Workspace management
# ---------------------------------------------------------------------------

ws_app = typer.Typer(help="Workspace management (create, finalize, prune)", no_args_is_help=True)
app.add_typer(ws_app, name="workspace")

from ws_ticket import main as _ws_ticket_main

ws_app.command(name="ticket")(_ws_ticket_main)

from wt_finalize import main as _wt_finalize_main

ws_app.command(name="finalize")(_wt_finalize_main)

from git_clean_them_all import main as _git_clean_main

ws_app.command(name="clean-all")(_git_clean_main)


# ---------------------------------------------------------------------------
# Group: run — Dev servers and test runners
# ---------------------------------------------------------------------------

run_app = typer.Typer(help="Dev servers and test runners", no_args_is_help=True)
app.add_typer(run_app, name="run")


@run_app.command()
def backend() -> None:
    """Start backend dev server."""
    ep_call("wt_run_backend")


@run_app.command()
def frontend() -> None:
    """Start frontend dev server."""
    ep_call("wt_run_frontend")


@run_app.command(name="build-frontend")
def build_frontend() -> None:
    """Build frontend app for production/testing."""
    ep_call("wt_build_frontend")


@run_app.command()
def tests() -> None:
    """Run project tests."""
    ep_call("wt_run_tests")


from verify_services import main as _verify_services_main

run_app.command(name="verify")(_verify_services_main)


# ---------------------------------------------------------------------------
# Group: ci — CI pipeline interaction
# ---------------------------------------------------------------------------

ci_app = typer.Typer(help="CI pipeline interaction", no_args_is_help=True)
app.add_typer(ci_app, name="ci")

from cancel_stale_pipelines import main as _cancel_pipelines_main

ci_app.command(name="cancel")(_cancel_pipelines_main)


@ci_app.command(name="trigger-e2e")
def trigger_e2e() -> None:
    """Trigger E2E tests on CI."""
    ep_call("wt_trigger_e2e")


@ci_app.command(name="fetch-errors")
def fetch_ci_errors() -> None:
    """Fetch error logs from a CI pipeline."""
    ep_call("wt_fetch_ci_errors")


@ci_app.command(name="fetch-failed-tests")
def fetch_failed_tests() -> None:
    """Extract failed test IDs from a CI pipeline."""
    ep_call("wt_fetch_failed_tests")


@ci_app.command(name="quality-check")
def quality_check() -> None:
    """Run quality analysis (SonarQube, coverage, etc.)."""
    ep_call("wt_quality_check")


# ---------------------------------------------------------------------------
# Group: db — Database operations
# ---------------------------------------------------------------------------

db_app = typer.Typer(help="Database operations", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command()
def refresh() -> None:
    """Re-import database from dump/DSLR."""
    lc = _get_lifecycle()
    lc.db_refresh()
    print(json.dumps(lc.status(), indent=2, default=str))


@db_app.command(name="restore-ci")
def restore_ci_db() -> None:
    """Restore database from CI dump."""
    ep_call("wt_restore_ci_db")


@db_app.command(name="reset-passwords")
def reset_passwords() -> None:
    """Reset all user passwords to a known value."""
    ep_call("wt_reset_passwords")


# ---------------------------------------------------------------------------
# Group: mr — Merge request and ticket workflow
# ---------------------------------------------------------------------------

mr_app = typer.Typer(help="Merge request and ticket workflow", no_args_is_help=True)
app.add_typer(mr_app, name="mr")

from create_mr import main as _create_mr_main

mr_app.command(name="create")(_create_mr_main)

from check_transition_gates import main as _check_gates_main

mr_app.command(name="check-gates")(_check_gates_main)

from fetch_issue_context import main as _fetch_issue_main

mr_app.command(name="fetch-issue")(_fetch_issue_main)

from detect_tenant import main as _detect_tenant_main

mr_app.command(name="detect-tenant")(_detect_tenant_main)

from collect_followup_data import main as _collect_followup_main

mr_app.command(name="followup")(_collect_followup_main)


# ---------------------------------------------------------------------------
# Project overlay commands (registered dynamically by project_hooks module)
# ---------------------------------------------------------------------------


def _tag_overlay_commands(overlay_name: str) -> None:
    """Append [overlay_name] to help of EP commands with project-layer overrides."""
    from lib.registry import active_layer

    ep_commands: dict[str, tuple[typer.Typer, str]] = {
        "wt_run_backend": (run_app, "backend"),
        "wt_run_frontend": (run_app, "frontend"),
        "wt_build_frontend": (run_app, "build-frontend"),
        "wt_run_tests": (run_app, "tests"),
        "wt_trigger_e2e": (ci_app, "trigger-e2e"),
        "wt_fetch_ci_errors": (ci_app, "fetch-errors"),
        "wt_fetch_failed_tests": (ci_app, "fetch-failed-tests"),
        "wt_quality_check": (ci_app, "quality-check"),
        "wt_restore_ci_db": (db_app, "restore-ci"),
        "wt_reset_passwords": (db_app, "reset-passwords"),
    }

    for ep_name, (sub_app_ref, cmd_name) in ep_commands.items():
        if active_layer(ep_name) == "project":
            for cmd in sub_app_ref.registered_commands:
                # cmd.name is None when inferred from function name
                resolved = cmd.name or (getattr(cmd.callback, "__name__", "").replace("_", "-") if cmd.callback else "")
                if resolved == cmd_name:
                    tag = f"[{overlay_name}]"
                    current = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
                    if tag not in current:  # pragma: no branch
                        cmd.help = f"{current} {tag}"


def _register_overlay_commands() -> None:
    """Let the project overlay register a named sub-app under t3."""
    try:
        from lib.project_hooks import create_cli_group  # ty: ignore[unresolved-import]

        name, help_text, overlay_app = create_cli_group()
        app.add_typer(overlay_app, name=name, help=help_text)
        _tag_overlay_commands(name)
    except ImportError:
        pass


_register_overlay_commands()


if __name__ == "__main__":
    app()
