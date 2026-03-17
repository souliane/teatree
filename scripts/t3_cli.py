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

Top-level commands (friction reducers):
    start        Zero to coding — fetch issue, create worktree, setup, start
    ship         Code done to MR — squash, push, cancel pipelines, create MR
    daily        Daily followup — collect, check gates, advance, remind
    status       Show all 3 FSM states (worktree, ticket, session)
    info         Show extension point registry (layers, handlers, overrides)

Groups:
    lifecycle    Worktree state machine (setup, start, teardown)
    workspace    Workspace management (create, finalize, prune)
    run          Dev servers and test runners
    test         Test runners with automatic environment setup
    ci           CI pipeline interaction
    db           Database operations
    mr           Merge request and ticket workflow
"""

import json
import sys
from pathlib import Path

import lib.init
import typer

lib.init.init()

from lib.env import detect_ticket_dir, resolve_context
from lib.fsm import generate_mermaid
from lib.lifecycle import WorktreeLifecycle
from lib.registry import call as ep_call
from lib.session_fsm import SessionPhase
from lib.ticket_fsm import TicketLifecycle

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


def _get_ticket() -> TicketLifecycle:
    td = detect_ticket_dir()
    if not td:
        print("Error: not in a ticket directory", file=sys.stderr)
        raise typer.Exit(1)
    return TicketLifecycle(ticket_dir=td)


def _get_session() -> SessionPhase:
    import hashlib
    import os

    td = detect_ticket_dir()
    # Scope session to ticket dir (quality gates are per-ticket, not per-conversation)
    session_id = hashlib.sha256(td.encode()).hexdigest()[:12] if td else "global"
    state_dir = os.environ.get("T3_SESSION_DIR", "/tmp/t3-sessions")  # noqa: S108
    return SessionPhase(session_id=session_id, state_dir=state_dir)


# ---------------------------------------------------------------------------
# High-level commands (friction reducers)
# ---------------------------------------------------------------------------


@app.command(name="full-status")
def full_status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show all 3 FSM states: worktree, ticket, and session."""
    data: dict[str, object] = {}

    td = detect_ticket_dir()
    if td:
        wt = WorktreeLifecycle(ticket_dir=td)
        tk = TicketLifecycle(ticket_dir=td)
        data["worktree"] = wt.state
        data["ticket"] = tk.state
    else:
        data["worktree"] = None
        data["ticket"] = None

    session = _get_session()
    data["session"] = session.state

    if as_json:
        print(json.dumps(data, indent=2))
    else:
        wt_state = data["worktree"] or "n/a"
        tk_state = data["ticket"] or "n/a"
        print(f"worktree: {wt_state} | ticket: {tk_state} | session: {data['session']}")


@app.command(name="start-ticket")
def start_ticket(
    issue_url: str = typer.Argument(help="Issue/ticket URL to start working on"),
    variant: str = typer.Option("", help="Tenant variant"),
) -> None:
    """Zero to coding — fetch issue, create worktree, setup, start services.

    Chains: t3-ticket → ws_ticket → lifecycle setup --start.
    Transitions ticket: not_started → scoped → started.
    """
    from rich.console import Console

    console = Console()

    # 1. Fetch issue context
    console.print("[bold]Step 1/3:[/] Fetching issue context...")
    ep_call("fetch_issue_context", issue_url)

    # 2. Create worktree (delegates to ws_ticket)
    console.print("[bold]Step 2/3:[/] Creating worktree...")
    ep_call("ws_create_ticket_worktree", issue_url, variant)

    # 3. Setup + start (lifecycle)
    console.print("[bold]Step 3/3:[/] Provisioning and starting services...")
    lc = _get_lifecycle()
    ctx = resolve_context()
    if lc.state == "created":
        lc.provision(wt_dir=ctx.wt_dir, main_repo=ctx.main_repo, variant=variant)
    if lc.state == "provisioned":
        lc.start_services()
        lc.verify()

    # Update ticket FSM
    tk = _get_ticket()
    if tk.state == "not_started":
        tk.scope(issue_url=issue_url)
    if tk.state == "scoped":
        tk.start(worktree_dirs=[ctx.wt_dir])

    console.print(f"\n[bold green]Ready![/] worktree: {lc.state} | ticket: {tk.state}")
    print(json.dumps({"worktree": lc.status(), "ticket": tk.status()}, indent=2, default=str))


@app.command()
def ship(
    force: bool = typer.Option(False, "--force", help="Skip quality gates (requires user approval)"),
) -> None:
    """Code done to MR — squash fixups, push, cancel stale pipelines, create MR.

    Transitions ticket: reviewed → shipped.
    """
    from rich.console import Console

    console = Console()

    # Check session gate
    session = _get_session()
    if not force and not session.has_visited("testing"):
        console.print("[bold red]⚠ Cannot ship:[/] session has not passed testing.")
        console.print("  Use --force to override (requires explicit user approval).")
        raise typer.Exit(1)
    if not force and not session.has_visited("reviewing"):
        console.print("[bold red]⚠ Cannot ship:[/] session has not passed reviewing.")
        console.print("  Use --force to override (requires explicit user approval).")
        raise typer.Exit(1)

    # 1. Cancel stale pipelines
    console.print("[bold]Step 1/3:[/] Cancelling stale pipelines...")
    ep_call("wt_cancel_stale_pipelines")

    # 2. Push
    console.print("[bold]Step 2/3:[/] Pushing to remote...")
    ep_call("wt_push")

    # 3. Create MR
    console.print("[bold]Step 3/3:[/] Creating merge request...")
    ep_call("wt_create_mr")

    # Update ticket FSM
    tk = _get_ticket()
    if tk.state == "reviewed":
        tk.ship(mr_urls=[])  # MR URLs would come from the create_mr output

    session.begin_shipping(force=force)
    console.print(f"\n[bold green]Shipped![/] ticket: {tk.state} | session: {session.state}")


@app.command()
def daily() -> None:
    """Daily followup — collect data, check gates, advance tickets, remind reviewers.

    Chains: collect_followup_data → check_transition_gates → review reminders.
    """
    from rich.console import Console

    console = Console()

    console.print("[bold]Step 1/3:[/] Collecting followup data...")
    ep_call("followup_collect")

    console.print("[bold]Step 2/3:[/] Checking transition gates...")
    ep_call("followup_check_gates")

    console.print("[bold]Step 3/3:[/] Sending review reminders...")
    ep_call("followup_remind_reviewers")

    console.print("[bold green]Daily followup complete.[/]")


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
    start_services: bool = typer.Option(False, "--start", help="Also start services after provisioning"),
) -> None:
    """Provision worktree: ports, env, symlinks, DB. Use --start to also start services."""
    lc = _get_lifecycle()
    ctx = resolve_context()
    lc.provision(wt_dir=ctx.wt_dir, main_repo=ctx.main_repo, variant=variant)
    if start_services:
        lc.start_services()
        lc.verify()
    print(json.dumps(lc.status(), indent=2, default=str))


@lc_app.command()
def start() -> None:
    """Start dev servers (backend + frontend), then verify.

    Auto-provisions first if worktree is in 'created' state.
    """
    lc = _get_lifecycle()
    if lc.state == "created":
        ctx = resolve_context()
        lc.provision(wt_dir=ctx.wt_dir, main_repo=ctx.main_repo, variant="")
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
# Group: test — Test runners with automatic setup
# ---------------------------------------------------------------------------

test_app = typer.Typer(help="Test runners with automatic environment setup", no_args_is_help=True)
app.add_typer(test_app, name="test")

from run_e2e import main as _run_e2e_main

test_app.command(name="e2e")(_run_e2e_main)


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


@mr_app.command(name="post-evidence")
def post_evidence(
    paths: list[str] = typer.Argument(help="Screenshot/file paths to upload"),
    mr_url: str = typer.Option("", "--mr", help="MR URL (auto-detected from branch if omitted)"),
    title: str = typer.Option("Test Evidence", "--title", help="Comment title"),
) -> None:
    """Upload screenshots/files and post as an MR comment.

    Handles GitLab upload + note creation with correct escaping.
    """
    ep_call("wt_post_mr_evidence", paths, mr_url, title)


# ---------------------------------------------------------------------------
# Group: config — Configuration introspection
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="Configuration introspection", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command()
def autoload() -> None:
    """List all skill auto-loading rules from context-match.yml files."""
    from lib.env import skill_dirs

    found = False
    for skill_dir, skill_name in skill_dirs():
        match_file = skill_dir / "hook-config" / "context-match.yml"
        if not match_file.is_file():
            continue
        found = True
        _print_autoload_rules(skill_name, match_file)

    if not found:
        print("No context-match.yml files found in any skill directory.")


def _parse_context_match(skill_name: str, match_file: Path) -> list[tuple[str, str, str]]:
    """Parse a context-match.yml into (type, skill, pattern) tuples."""
    section = ""
    current_skill = ""
    rows: list[tuple[str, str, str]] = []

    with match_file.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip()
            stripped = line.lstrip()

            if not stripped or stripped.startswith("#"):
                continue

            if stripped.startswith("cwd_patterns:"):
                section = "trigger"
                continue
            if stripped.startswith("companion_skills:"):
                section = "companion"
                continue

            # Top-level key that isn't a known section = end current section
            if not line.startswith(" ") and not line.startswith("\t"):
                section = ""
                continue

            if section == "trigger" and stripped.startswith("- "):
                pattern = stripped[2:].strip().strip('"').strip("'")
                rows.append(("trigger", skill_name, pattern))
            elif section == "companion":
                if not stripped.startswith("- "):
                    current_skill = stripped.rstrip(":").strip()
                else:
                    pattern = stripped[2:].strip().strip('"').strip("'")
                    rows.append(("companion", current_skill, pattern))
    return rows


def _print_autoload_rules(skill_name: str, match_file: Path) -> None:
    """Parse and display a single context-match.yml."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"\n[bold cyan]{skill_name}[/] — {match_file}")

    rows = _parse_context_match(skill_name, match_file)
    if rows:
        table = Table(show_header=True)
        table.add_column("Type", style="green", width=10)
        table.add_column("Skill", style="cyan")
        table.add_column("Pattern", style="yellow")
        for row_type, skill, pattern in rows:
            table.add_row(row_type, skill, pattern)
        console.print(table)


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
