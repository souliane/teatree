import os
import subprocess  # noqa: S404
import time
from pathlib import Path
from subprocess import Popen  # noqa: S404
from typing import IO

import typer
from django.core.management.base import OutputWrapper
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, RunCommand
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.core.worktree_env import write_env_worktree


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
    subprocess.run(  # noqa: S603
        ["direnv", "allow", wt_path],
        capture_output=True,
        check=False,
    )
    if (Path(wt_path) / ".pre-commit-config.yaml").is_file():
        stdout.write("  Running: prek install")
        subprocess.run(
            ["prek", "install", "-f"],
            cwd=wt_path,
            capture_output=True,
            check=False,
        )


def _register_new_repos(worktree: Worktree, stdout: OutputWrapper) -> None:
    """Discover git worktrees in the ticket directory that aren't in the DB yet.

    When a user manually runs ``git worktree add`` inside a ticket directory,
    the new repo has no Worktree record.  This function scans the ticket
    directory for subdirectories that look like git worktrees (``.git`` is a
    file, not a directory) and creates missing records under the same ticket.
    """
    ticket_or_none = worktree.ticket
    if ticket_or_none is None:
        return
    ticket_dir = (worktree.extra or {}).get("worktree_path", "")
    if not ticket_dir:
        return
    # The ticket directory is the parent of the repo worktree path
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
        # .git as a file = git worktree; .git as a dir = main clone (skip)
        if git_marker.is_dir():
            continue
        entry_str = str(entry)
        if entry_str in known_paths:
            continue
        # New repo discovered — create a Worktree record
        Worktree.objects.create(
            ticket=ticket,
            repo_path=entry.name,
            branch=worktree.branch,
            extra={"worktree_path": entry_str},
        )
        stdout.write(f"  Discovered new repo: {entry.name}")


class Command(TyperCommand):
    _DB_IMPORT_MAX_FAILURES = 3

    @command()
    def setup(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant. Updates ticket if provided."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
        force: bool = typer.Option(default=False, help="Bypass DB import circuit breaker."),  # noqa: FBT001
    ) -> int:
        """Provision a worktree (allocate ports, DB name, run overlay steps).

        Discovers repos added to the ticket directory since initial creation
        and provisions all worktrees for the ticket, not just the resolved one.
        """
        # Guard against typer.Option defaults when called as a Python method
        # (typer.Option("") evaluates to OptionInfo, not "")
        if not isinstance(variant, str):
            variant = ""
        if not isinstance(overlay, str):
            overlay = ""
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=worktree.ticket.pk)

        if variant and ticket.variant != variant:
            ticket.variant = variant
            ticket.save(update_fields=["variant"])

        # Discover repos added to the ticket directory since initial creation
        _register_new_repos(worktree, self.stdout)

        resolved_overlay = get_overlay()

        # Provision ALL worktrees for the ticket (fault-tolerant per worktree)
        failed_repos: list[str] = []
        for wt in ticket.worktrees.all():
            try:
                self._provision_worktree(wt, resolved_overlay, force=force)
            except Exception as exc:  # noqa: BLE001
                failed_repos.append(wt.repo_path)
                self.stderr.write(f"  ERROR provisioning {wt.repo_path}: {exc}")

        if failed_repos:
            self.stderr.write(f"  {len(failed_repos)} worktree(s) failed: {', '.join(failed_repos)}")

        _write_skill_metadata_cache()

        return int(worktree.pk)

    def _provision_worktree(self, worktree: Worktree, overlay: "OverlayBase", *, force: bool = False) -> None:
        self.stdout.write(f"  Provisioning: {worktree.repo_path}")

        if worktree.state == Worktree.State.CREATED:
            worktree.provision()
            worktree.save()
        else:
            worktree.refresh_ports_if_needed()

        envfile = write_env_worktree(worktree)
        if envfile:
            self.stdout.write(f"  Written: {envfile}")

        _setup_worktree_dir((worktree.extra or {}).get("worktree_path", ""), worktree, overlay, self.stdout)

        # Import database (DSLR/dump fallback chain) before running provision steps
        if overlay.get_db_import_strategy(worktree) is not None:
            extra = worktree.extra or {}
            failures = extra.get("db_import_failures", 0)
            if failures >= self._DB_IMPORT_MAX_FAILURES and not force:
                self.stderr.write(f"  SKIPPED: DB import (failed {failures} consecutive times). Use --force to retry.")
            else:
                self.stdout.write("  Running: db-import")
                env = {**os.environ, **overlay.get_env_extra(worktree)}
                env.pop("VIRTUAL_ENV", None)
                os.environ.update(env)  # pg tools need these to connect
                if overlay.db_import(worktree):
                    extra.pop("db_import_failures", None)
                    worktree.extra = extra
                    worktree.save(update_fields=["extra"])
                else:
                    extra["db_import_failures"] = failures + 1
                    worktree.extra = extra
                    worktree.save(update_fields=["extra"])
                    self.stderr.write("  WARNING: DB import failed. Continuing with provision steps...")

        for step in overlay.get_provision_steps(worktree):
            self.stdout.write(f"  Running: {step.name}")
            step.callable()

        self._run_post_db_steps(overlay, worktree)

        # Run pre-run steps for all services (e.g. frontend translation sync)
        for service_name in overlay.get_run_commands(worktree):
            for step in overlay.get_pre_run_steps(worktree, service_name):
                self.stdout.write(f"  Running: {step.name}")
                step.callable()

    def _run_post_db_steps(self, overlay: OverlayBase, worktree: Worktree) -> None:
        for step in overlay.get_post_db_steps(worktree):
            self.stdout.write(f"  Running: {step.name}")
            step.callable()

        reset_step = overlay.get_reset_passwords_command(worktree)
        if reset_step:
            self.stdout.write("  Running: reset-passwords")
            reset_step.callable()

    def _start_docker_services(self, worktree: Worktree, overlay: "OverlayBase") -> None:
        for name, spec in overlay.get_services_config(worktree).items():
            start_cmd = spec.get("start_command", [])
            if start_cmd:
                self.stdout.write(f"  Starting {name}...")
                subprocess.run(start_cmd, check=False, capture_output=True)  # noqa: S603

    def _launch_app_processes(
        self,
        commands: dict[str, list[str] | RunCommand],
        env: dict[str, str],
        log_dir: Path,
    ) -> tuple[dict[str, int], list[str]]:
        pids: dict[str, int] = {}
        failed: list[str] = []
        log_files: list[IO] = []
        for service_name, raw_cmd in commands.items():
            if isinstance(raw_cmd, RunCommand):
                cmd = raw_cmd.args
                cwd: str | Path | None = raw_cmd.cwd
            else:
                cmd = raw_cmd
                cwd = None
            log_path = log_dir / f"{service_name}.log"
            self.stdout.write(f"  Launching {service_name} (log: {log_path})")
            log_file = log_path.open("w")
            log_files.append(log_file)
            proc = Popen(cmd, env=env, cwd=cwd, stdout=log_file, stderr=log_file, start_new_session=True)  # noqa: S603
            pids[service_name] = proc.pid
            time.sleep(1)
            if proc.poll() is not None:
                self.stderr.write(f"  ERROR: {service_name} exited immediately (code {proc.returncode})")
                failed.append(service_name)
        for f in log_files:
            f.close()
        return pids, failed

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
    ) -> str:
        """Start Docker services + app servers (background), then transition FSM.

        Safe to re-run: kills existing processes before launching new ones.
        """
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()

        # Kill stale processes from a previous start (safe if none exist)
        self._kill_worktree_processes(worktree)

        self._start_docker_services(worktree, resolved_overlay)

        commands = resolved_overlay.get_run_commands(worktree)
        for service_name in commands:
            for step in resolved_overlay.get_pre_run_steps(worktree, service_name):
                self.stdout.write(f"  Preparing: {step.name}")
                step.callable()

        write_env_worktree(worktree)
        env = {**os.environ, **resolved_overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        ticket_dir = (worktree.extra or {}).get("worktree_path", "")
        log_dir = Path(ticket_dir) / "logs" if ticket_dir else Path("/tmp")  # noqa: S108
        log_dir.mkdir(parents=True, exist_ok=True)

        pids, failed = self._launch_app_processes(commands, env, log_dir)

        worktree.start_services(services=list(commands))
        extra = dict(worktree.extra or {})
        extra["pids"] = pids
        if failed:
            extra["failed_services"] = failed
        worktree.extra = extra
        worktree.save()

        if failed:
            self.stderr.write(f"  WARNING: {len(failed)} service(s) failed: {', '.join(failed)}")

        return worktree.state

    @command()
    def status(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> dict[str, str]:
        worktree = resolve_worktree(path)
        return {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
        }

    def _kill_worktree_processes(self, worktree: Worktree) -> list[str]:
        """Kill app processes from a previous ``start`` run.

        Returns the list of service names whose PIDs were found and signalled.
        """
        import signal as sig  # noqa: PLC0415

        extra = dict(worktree.extra or {})
        pids: dict[str, int] = extra.get("pids", {})
        killed: list[str] = []
        for service_name, pid in pids.items():
            try:
                os.kill(pid, sig.SIGTERM)
                killed.append(service_name)
                self.stdout.write(f"  Stopped {service_name} (pid {pid})")
            except ProcessLookupError:
                self.stdout.write(f"  {service_name} already stopped (pid {pid})")
            except PermissionError:
                self.stderr.write(f"  WARNING: cannot kill {service_name} (pid {pid}) — permission denied")
        if killed:
            time.sleep(2)  # give processes time to clean up
        extra.pop("pids", None)
        extra.pop("failed_services", None)
        worktree.extra = extra
        worktree.save()
        return killed

    @command()
    def restart(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
    ) -> str:
        """Kill existing processes and restart all services.

        Use after ``git pull`` or code changes when the worktree is already
        provisioned.  Re-runs pre-run steps (patches customer.json, syncs
        translations, etc.) and launches fresh processes.
        """
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)

        if worktree.state == Worktree.State.CREATED:
            self.stdout.write("  Worktree not provisioned — running setup + start instead.")
            self.setup(path=path, variant="", overlay=overlay)
            return self.start(path=path, overlay=overlay)

        resolved_overlay = get_overlay()

        # 1. Kill old processes
        self._kill_worktree_processes(worktree)

        # 2. Start docker services (idempotent)
        self._start_docker_services(worktree, resolved_overlay)

        # 3. Re-run pre-run steps and launch fresh
        commands = resolved_overlay.get_run_commands(worktree)
        for service_name in commands:
            for step in resolved_overlay.get_pre_run_steps(worktree, service_name):
                self.stdout.write(f"  Preparing: {step.name}")
                step.callable()

        write_env_worktree(worktree)
        env = {**os.environ, **resolved_overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        ticket_dir = (worktree.extra or {}).get("worktree_path", "")
        log_dir = Path(ticket_dir) / "logs" if ticket_dir else Path("/tmp")  # noqa: S108
        log_dir.mkdir(parents=True, exist_ok=True)

        pids, failed = self._launch_app_processes(commands, env, log_dir)

        worktree.start_services(services=list(commands))
        extra = dict(worktree.extra or {})
        extra["pids"] = pids
        if failed:
            extra["failed_services"] = failed
        worktree.extra = extra
        worktree.save()

        if failed:
            self.stderr.write(f"  WARNING: {len(failed)} service(s) failed: {', '.join(failed)}")
            return f"restarted with {len(failed)} failure(s)"

        return worktree.state

    @command()
    def teardown(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        worktree = resolve_worktree(path)
        worktree.teardown()
        worktree.save()
        return worktree.state

    @command()
    def clean(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Teardown worktree — stop services, drop DB, clean state."""
        worktree = resolve_worktree(path)
        worktree.teardown()
        worktree.save()
        return f"Cleaned worktree {worktree.repo_path} ({worktree.state})"

    @command(name="smoke-test")
    def smoke_test(self) -> dict[str, object]:
        """Quick health check: overlay loads, CLI responds, imports OK."""
        checks: dict[str, object] = {}

        # 1. Overlay loads
        try:
            overlay = get_overlay()
            checks["overlay"] = {"status": "ok", "repos": overlay.get_repos()}
        except Exception as exc:  # noqa: BLE001
            checks["overlay"] = {"status": "error", "detail": str(exc)}

        # 2. CLI responds (t3 --help)
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

        # 3. DB accessible
        try:
            count = Worktree.objects.count()
            checks["database"] = {"status": "ok", "worktrees": count}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"status": "error", "detail": str(exc)}

        # 4. Pre-commit hooks parseable
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

    @command()
    def diagram(self, model: str = "worktree") -> str:
        """Print a state diagram as Mermaid. Models: worktree, ticket, task."""
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
