import json
import os
import subprocess  # noqa: S404
import time
from pathlib import Path
from subprocess import Popen  # noqa: S404
from typing import IO

import typer
from django.core.management.base import OutputWrapper
from django_typer.management import TyperCommand, command

from teatree.config import DATA_DIR
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
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
    metadata = get_overlay().metadata.get_skill_metadata()
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


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
    @command()
    def setup(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant. Updates ticket if provided."),
    ) -> int:
        """Provision a worktree (allocate ports, DB name, run overlay steps).

        Discovers repos added to the ticket directory since initial creation
        and provisions all worktrees for the ticket, not just the resolved one.
        """
        worktree = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=worktree.ticket.pk)

        if variant and ticket.variant != variant:
            ticket.variant = variant
            ticket.save(update_fields=["variant"])

        # Discover repos added to the ticket directory since initial creation
        _register_new_repos(worktree, self.stdout)

        overlay = get_overlay()

        # Provision ALL worktrees for the ticket
        for wt in ticket.worktrees.all():
            self._provision_worktree(wt, overlay)

        _write_skill_metadata_cache()

        return int(worktree.pk)

    def _provision_worktree(self, worktree: Worktree, overlay: "OverlayBase") -> None:
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
            self.stdout.write("  Running: db-import")
            env = {**os.environ, **overlay.get_env_extra(worktree)}
            env.pop("VIRTUAL_ENV", None)
            os.environ.update(env)  # pg tools need these to connect
            if not overlay.db_import(worktree):
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
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        for post_step in overlay.get_post_db_steps(worktree):
            cmd = post_step.get("command", "")
            if cmd:
                self.stdout.write(f"  Running: {post_step.get('name', '?')}")
                subprocess.run(cmd, shell=True, check=False, env=env)  # noqa: S602

        reset_cmd = overlay.get_reset_passwords_command(worktree)
        if reset_cmd:
            self.stdout.write("  Running: reset-passwords")
            subprocess.run(reset_cmd, shell=True, check=False, env=env)  # noqa: S602

    @command()
    def start(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start Docker services + app servers (background), then transition FSM."""
        worktree = resolve_worktree(path)
        overlay = get_overlay()

        # 1. Start Docker services (DB, Redis)
        for name, spec in overlay.get_services_config(worktree).items():
            start_cmd = spec.get("start_command", "")
            if start_cmd:
                self.stdout.write(f"  Starting {name}...")
                subprocess.run(start_cmd, shell=True, check=False, capture_output=True)  # noqa: S602

        # 2. Run pre-run steps for each service
        commands = overlay.get_run_commands(worktree)
        for service_name in commands:
            for step in overlay.get_pre_run_steps(worktree, service_name):
                self.stdout.write(f"  Preparing: {step.name}")
                step.callable()

        # 3. Build env and launch app services as background processes
        # Ports were assigned during setup — don't reallocate here
        write_env_worktree(worktree)
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        ticket_dir = (worktree.extra or {}).get("worktree_path", "")
        log_dir = Path(ticket_dir) / "logs" if ticket_dir else Path("/tmp")  # noqa: S108
        log_dir.mkdir(parents=True, exist_ok=True)

        pids: dict[str, int] = {}
        failed: list[str] = []
        log_files: list[IO] = []  # keep file handles alive until after FSM save
        for service_name, cmd in commands.items():
            log_path = log_dir / f"{service_name}.log"
            self.stdout.write(f"  Launching {service_name} (log: {log_path})")
            log_file = log_path.open("w")
            log_files.append(log_file)
            proc = Popen(cmd, shell=True, env=env, stdout=log_file, stderr=log_file, start_new_session=True)  # noqa: S602
            pids[service_name] = proc.pid
            time.sleep(1)
            if proc.poll() is not None:
                self.stderr.write(f"  ERROR: {service_name} exited immediately (code {proc.returncode})")
                failed.append(service_name)

        # 4. Transition FSM and store PIDs
        worktree.start_services(services=list(commands))
        extra = dict(worktree.extra or {})
        extra["pids"] = pids
        if failed:
            extra["failed_services"] = failed
        worktree.extra = extra
        worktree.save()

        # Close log file handles now that processes have their own copies
        for f in log_files:
            f.close()

        if failed:
            self.stderr.write(f"  WARNING: {len(failed)} service(s) failed: {', '.join(failed)}")

        return worktree.state

    @command()
    def status(
        self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")
    ) -> dict[str, str]:
        worktree = resolve_worktree(path)
        return {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
        }

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
