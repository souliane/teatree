import os
import subprocess  # noqa: S404
from pathlib import Path

import typer
from django.core.management.base import OutputWrapper
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.core.step_runner import ProvisionReport, run_provision_steps, run_step
from teatree.core.worktree_env import write_env_worktree
from teatree.utils.ports import find_free_ports, get_worktree_ports


def _append_envrc_lines(wt_path: str, lines: list[str]) -> None:
    """Append missing lines to the worktree .envrc (idempotent)."""
    envrc = Path(wt_path) / ".envrc"
    existing = envrc.read_text() if envrc.is_file() else ""
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        envrc.write_text(existing.rstrip() + "\n" + "\n".join(missing) + "\n")


def _write_skill_metadata_cache() -> None:
    """Write overlay skill metadata to XDG cache for hook consumption."""
    from teatree.core.views._startup import _write_skill_metadata_cache as _write  # noqa: PLC0415

    _write()


def _setup_worktree_dir(wt_path: str, worktree: Worktree, overlay: OverlayBase, stdout: OutputWrapper) -> None:
    """Configure direnv and pre-commit for the worktree directory."""
    if not wt_path or not Path(wt_path).is_dir():
        return
    _append_envrc_lines(wt_path, overlay.get_envrc_lines(worktree))
    result = run_step("direnv-allow", ["direnv", "allow", wt_path], check=False)
    if not result.success:
        stdout.write(f"  direnv allow: {result.error}")
    if (Path(wt_path) / ".pre-commit-config.yaml").is_file():
        stdout.write("  Running: prek install")
        result = run_step("prek-install", ["prek", "install", "-f"], cwd=wt_path, check=False)
        if not result.success:
            stdout.write(f"  prek install: {result.error}")


def _register_new_repos(worktree: Worktree, stdout: OutputWrapper) -> None:
    """Discover git worktrees in the ticket directory that aren't in the DB yet."""
    ticket_or_none = worktree.ticket
    if ticket_or_none is None:
        return
    ticket_dir = (worktree.extra or {}).get("worktree_path", "")
    if not ticket_dir:
        return
    ticket_path = Path(ticket_dir).parent
    if not ticket_path.is_dir():
        return

    ticket = Ticket.objects.get(pk=ticket_or_none.pk)
    known_paths = {(wt.extra or {}).get("worktree_path", "") for wt in ticket.worktrees.all()}

    for entry in sorted(ticket_path.iterdir()):
        if not entry.is_dir():
            continue
        git_marker = entry / ".git"
        if not git_marker.exists():
            continue
        if git_marker.is_dir():
            continue
        entry_str = str(entry)
        if entry_str in known_paths:
            continue
        Worktree.objects.create(
            ticket=ticket,
            repo_path=entry.name,
            branch=worktree.branch,
            extra={"worktree_path": entry_str},
        )
        stdout.write(f"  Discovered new repo: {entry.name}")


def _resolve_typer_defaults(
    variant: "str | object", overlay: "str | object", verbose: "bool | object"
) -> tuple[str, str, bool]:
    return (
        variant if isinstance(variant, str) else "",
        overlay if isinstance(overlay, str) else "",
        verbose if isinstance(verbose, bool) else False,
    )


def _update_ticket_variant(ticket: "Ticket", variant: str) -> None:
    """Update ticket variant and recompute db_name for all worktrees."""
    if not variant or ticket.variant == variant:
        return
    ticket.variant = variant
    ticket.save(update_fields=["variant"])
    for wt in ticket.worktrees.all():  # type: ignore[attr-defined]
        old_db = wt.db_name
        wt.db_name = wt._build_db_name()  # noqa: SLF001
        if wt.db_name != old_db:
            wt.save(update_fields=["db_name"])


def _compose_project(worktree: Worktree) -> str:
    """Return the docker-compose project name for this worktree."""
    ticket = worktree.ticket
    return f"{worktree.repo_path}-wt{ticket.ticket_number}" if ticket else worktree.repo_path


def _compose_env(ports: dict[str, int]) -> dict[str, str]:
    """Build env vars for docker-compose port mapping."""
    return {
        "BACKEND_HOST_PORT": str(ports.get("backend", 8000)),
        "FRONTEND_HOST_PORT": str(ports.get("frontend", 4200)),
        "POSTGRES_HOST_PORT": str(ports.get("postgres", 5432)),
        "REDIS_HOST_PORT": str(ports.get("redis", 6379)),
    }


def _docker_compose_down(project: str, stdout: OutputWrapper) -> None:
    """Stop and remove containers for the compose project."""
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", project, "down", "--remove-orphans"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stdout.write(f"  docker compose down: {result.stderr.strip()[:300]}")


def _docker_compose_up(
    project: str,
    compose_file: str,
    env: dict[str, str],
    stdout: OutputWrapper,
    stderr: OutputWrapper,
) -> bool:
    """Start all services via docker-compose."""
    cmd = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        compose_file,
        "up",
        "-d",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        stderr.write(f"  docker compose up failed: {result.stderr.strip()[:500]}")
        return False
    stdout.write("  docker compose up -d: OK")
    return True


class Command(TyperCommand):
    _DB_IMPORT_MAX_FAILURES = 3
    _verbose: bool = False

    @command()
    def setup(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant. Updates ticket if provided."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
        force: bool = typer.Option(default=False, help="Bypass DB import circuit breaker."),  # noqa: FBT001
        verbose: bool = typer.Option(default=False, help="Show step stdout/stderr."),  # noqa: FBT001
    ) -> int:
        """Provision a worktree (DB name, env file, overlay steps). No port allocation."""
        variant, overlay, verbose = _resolve_typer_defaults(variant, overlay, verbose)
        self._verbose = verbose
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=worktree.ticket.pk)

        _update_ticket_variant(ticket, variant)
        _register_new_repos(worktree, self.stdout)

        resolved_overlay = get_overlay()

        failed_repos: list[str] = []
        for wt in ticket.worktrees.all():
            try:
                report = self._provision_worktree(wt, resolved_overlay, force=force)
                if not report.success:
                    failed_repos.append(wt.repo_path)
            except Exception as exc:  # noqa: BLE001
                failed_repos.append(wt.repo_path)
                self.stderr.write(f"  ERROR provisioning {wt.repo_path}: {exc}")

        if failed_repos:
            self.stderr.write(f"  {len(failed_repos)} worktree(s) failed: {', '.join(failed_repos)}")

        _write_skill_metadata_cache()
        return int(worktree.pk)

    def _provision_worktree(
        self, worktree: Worktree, overlay: "OverlayBase", *, force: bool = False
    ) -> ProvisionReport:
        self.stdout.write(f"  Provisioning: {worktree.repo_path}")

        if worktree.state == Worktree.State.CREATED:
            worktree.provision()
            worktree.save()

        envfile = write_env_worktree(worktree)
        if envfile:
            self.stdout.write(f"  Written: {envfile}")

        _setup_worktree_dir((worktree.extra or {}).get("worktree_path", ""), worktree, overlay, self.stdout)

        if overlay.get_db_import_strategy(worktree) is not None:
            self._run_db_import(worktree, overlay, force=force)

        provision_report = run_provision_steps(
            overlay.get_provision_steps(worktree),
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
        )

        post_db_report = self._run_post_db_steps(overlay, worktree)

        pre_run_steps = []
        for service_name in overlay.get_run_commands(worktree):
            pre_run_steps.extend(overlay.get_pre_run_steps(worktree, service_name))
        pre_run_report = run_provision_steps(
            pre_run_steps,
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
            stop_on_required_failure=False,
        )

        self._run_health_checks(worktree, overlay)

        combined = ProvisionReport(
            steps=provision_report.steps + post_db_report.steps + pre_run_report.steps,
        )
        if self._verbose:
            self.stdout.write(combined.summary())
        return combined

    def _run_db_import(self, worktree: Worktree, overlay: OverlayBase, *, force: bool = False) -> None:
        extra = worktree.extra or {}
        failures = extra.get("db_import_failures", 0)
        if failures >= self._DB_IMPORT_MAX_FAILURES and not force:
            self.stderr.write(f"  SKIPPED: DB import (failed {failures} consecutive times). Use --force to retry.")
            return
        self.stdout.write("  Running: db-import")
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        os.environ.update(env)
        if overlay.db_import(worktree):
            extra.pop("db_import_failures", None)
            worktree.extra = extra
            worktree.save(update_fields=["extra"])
        else:
            extra["db_import_failures"] = failures + 1
            worktree.extra = extra
            worktree.save(update_fields=["extra"])
            self.stderr.write("  WARNING: DB import failed. Continuing with provision steps...")

    def _run_post_db_steps(self, overlay: OverlayBase, worktree: Worktree) -> ProvisionReport:
        steps = list(overlay.get_post_db_steps(worktree))
        reset_step = overlay.get_reset_passwords_command(worktree)
        if reset_step:
            steps.append(reset_step)
        return run_provision_steps(
            steps,
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
            stop_on_required_failure=False,
        )

    def _run_health_checks(self, worktree: Worktree, overlay: OverlayBase) -> None:
        checks = overlay.get_health_checks(worktree)
        if not checks:
            return
        failures: list[str] = []
        for check in checks:
            try:
                if not check.check():
                    failures.append(check.name)
                    self.stderr.write(f"  HEALTH CHECK FAILED: {check.name} — {check.description}")
                elif self._verbose:
                    self.stdout.write(f"  HEALTH CHECK OK: {check.name}")
            except Exception as exc:  # noqa: BLE001
                failures.append(check.name)
                self.stderr.write(f"  HEALTH CHECK ERROR: {check.name} — {exc}")
        if failures:
            self.stderr.write(f"  {len(failures)}/{len(checks)} health check(s) failed.")

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
    ) -> str:
        """Start all services via docker-compose with dynamically allocated ports.

        Finds free host ports at runtime, passes them to docker-compose,
        and starts all containers.  Safe to re-run (runs compose down first).
        """
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()
        project = _compose_project(worktree)

        # 1. Stop previous containers
        _docker_compose_down(project, self.stdout)

        # 2. Run pre-run steps (translation sync, customer.json patch, etc.)
        commands = resolved_overlay.get_run_commands(worktree)
        pre_run_steps = []
        for service_name in commands:
            pre_run_steps.extend(resolved_overlay.get_pre_run_steps(worktree, service_name))
        run_provision_steps(
            pre_run_steps,
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
            stop_on_required_failure=False,
        )

        # 3. Write non-port env file (variant, DB name, compose project)
        write_env_worktree(worktree)

        # 4. Allocate free host ports at runtime
        from teatree.config import load_config  # noqa: PLC0415

        workspace_dir = str(load_config().user.workspace_dir)
        ports = find_free_ports(workspace_dir)
        self.stdout.write(f"  Ports: {ports}")

        # 5. Start all services via docker-compose
        compose_file = resolved_overlay.get_compose_file(worktree)
        if not compose_file:
            self.stderr.write("  ERROR: No docker-compose file found.")
            return "error"

        env = {**os.environ, **resolved_overlay.get_env_extra(worktree), **_compose_env(ports)}
        env.pop("VIRTUAL_ENV", None)
        ok = _docker_compose_up(project, compose_file, env, self.stdout, self.stderr)

        if not ok:
            return "error"

        # 6. FSM transition
        worktree.start_services(services=list(commands))
        worktree.save()

        return worktree.state

    @command()
    def status(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> dict[str, object]:
        worktree = resolve_worktree(path)
        project = _compose_project(worktree)
        ports = get_worktree_ports(project)
        return {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
            "ports": ports,
        }

    @command()
    def restart(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
    ) -> str:
        """Stop containers, allocate fresh ports, and restart all services."""
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)

        if worktree.state == Worktree.State.CREATED:
            self.stdout.write("  Worktree not provisioned — running setup + start instead.")
            self.setup(path=path, variant="", overlay=overlay)
            return self.start(path=path, overlay=overlay)

        return self.start(path=path, overlay=overlay)

    @command()
    def teardown(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        worktree = resolve_worktree(path)
        project = _compose_project(worktree)
        _docker_compose_down(project, self.stdout)
        worktree.teardown()
        worktree.save()
        return worktree.state

    @command()
    def clean(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Teardown worktree — stop containers, drop DB, clean state."""
        worktree = resolve_worktree(path)
        project = _compose_project(worktree)
        _docker_compose_down(project, self.stdout)
        worktree.teardown()
        worktree.save()
        return f"Cleaned worktree {worktree.repo_path} ({worktree.state})"

    @command(name="smoke-test")
    def smoke_test(self) -> dict[str, object]:
        """Quick health check: overlay loads, CLI responds, imports OK."""
        checks: dict[str, object] = {}

        try:
            overlay = get_overlay()
            checks["overlay"] = {"status": "ok", "repos": overlay.get_repos()}
        except Exception as exc:  # noqa: BLE001
            checks["overlay"] = {"status": "error", "detail": str(exc)}

        try:
            result = subprocess.run(
                ["uv", "run", "t3", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            checks["cli"] = {"status": "ok" if result.returncode == 0 else "error"}
        except subprocess.TimeoutExpired:
            checks["cli"] = {"status": "error", "detail": "t3 --help timed out"}

        try:
            count = Worktree.objects.count()
            checks["database"] = {"status": "ok", "worktrees": count}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"status": "error", "detail": str(exc)}

        hook_config = Path("." if Path(".pre-commit-config.yaml").is_file() else os.environ.get("PWD", "."))
        hook_file = hook_config / ".pre-commit-config.yaml"
        if hook_file.is_file():
            try:
                from importlib import import_module  # noqa: PLC0415

                yaml = import_module("yaml")
                yaml.safe_load(hook_file.read_text(encoding="utf-8"))
                checks["hooks"] = {"status": "ok"}
            except Exception as exc:  # noqa: BLE001
                checks["hooks"] = {"status": "error", "detail": str(exc)}
        else:
            checks["hooks"] = {"status": "skipped", "detail": "no .pre-commit-config.yaml"}

        return checks

    @command(name="start-full")
    def start_full(
        self,
        issue_url: str = typer.Argument(help="Issue/ticket URL."),
        variant: str = typer.Option("", help="Tenant variant."),
        repos: str = typer.Option("", help="Comma-separated repo names (default: overlay repos)."),
        description: str = typer.Option("", help="Short description for the branch name."),
    ) -> str:
        """Zero to coding — create ticket, provision worktrees, start services."""
        from teatree.core.management.commands.workspace import Command as WorkspaceCommand  # noqa: PLC0415

        ws = WorkspaceCommand()
        ws.stdout = self.stdout
        ws.stderr = self.stderr
        ticket_id = ws.ticket(issue_url, variant=variant, repos=repos, description=description)
        if not ticket_id:
            return "Failed to create ticket."

        ticket = Ticket.objects.get(pk=ticket_id)
        first_wt = ticket.worktrees.first()
        if not first_wt:
            return f"Ticket #{ticket_id} created but no worktrees."

        wt_path = (first_wt.extra or {}).get("worktree_path", "")
        self.setup(path=wt_path, variant=variant)
        self.start(path=wt_path)

        return f"Ticket #{ticket_id} ready — services running in {wt_path}"

    @command(name="visit-phase")
    def visit_phase(self, ticket_id: int, phase: str) -> str:
        """Mark a phase as visited on the ticket's latest session."""
        from teatree.core.models import Session  # noqa: PLC0415

        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket)
        session.visit_phase(phase)
        return f"Phase '{phase}' marked as visited on session {session.pk}"

    @command()
    def diagram(self, model: str = "worktree", ticket: int | None = None) -> str:
        """Print a state diagram as Mermaid. Models: worktree, ticket, task."""
        if ticket is not None:
            from teatree.core.selectors import build_ticket_lifecycle_mermaid  # noqa: PLC0415

            return build_ticket_lifecycle_mermaid(ticket)

        model_map: dict[str, type] = {"worktree": Worktree, "ticket": Ticket}
        if model == "task":
            return _task_diagram()
        if model not in model_map:
            return f"Unknown model: {model}. Choose from: worktree, ticket, task"
        return _fsm_diagram(model_map[model])


def _fsm_diagram(model: type) -> str:
    """Generate a Mermaid state diagram from django-fsm transitions."""
    field = model._meta.get_field("state")  # type: ignore[attr-defined]  # noqa: SLF001
    default = field.default
    lines = ["stateDiagram-v2", f"    [*] --> {default}"]

    for t in field.get_all_transitions(model):
        source = t.source
        target = t.target
        if source == "*":
            for choice_val, _label in field.choices:
                lines.append(f"    {choice_val} --> {target}: {t.name}()")
        else:
            lines.append(f"    {source} --> {target}: {t.name}()")

    return "\n".join(lines)


def _task_diagram() -> str:
    """Task uses manual status management, not FSM transitions."""
    lines = [
        "stateDiagram-v2",
        "    [*] --> pending",
        "    pending --> claimed: claim()",
        "    claimed --> completed: complete()",
        "    claimed --> failed: fail()",
        "    completed --> [*]",
        "    failed --> [*]",
    ]
    return "\n".join(lines)
