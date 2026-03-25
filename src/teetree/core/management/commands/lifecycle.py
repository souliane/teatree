import json
import os
import subprocess  # noqa: S404
from pathlib import Path

import typer
from django_typer.management import TyperCommand, command

from teetree.config import DATA_DIR
from teetree.core.models import Ticket, Worktree
from teetree.core.overlay_loader import get_overlay
from teetree.core.resolve import resolve_worktree
from teetree.core.worktree_env import write_env_worktree


def _append_envrc_lines(wt_path: str, lines: list[str]) -> None:
    """Append missing lines to the worktree .envrc (idempotent)."""
    envrc = Path(wt_path) / ".envrc"
    existing = envrc.read_text() if envrc.is_file() else ""
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        envrc.write_text(existing.rstrip() + "\n" + "\n".join(missing) + "\n")


def _write_skill_metadata_cache() -> None:
    """Write overlay skill metadata to XDG cache for hook consumption."""
    metadata = get_overlay().get_skill_metadata()
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


class Command(TyperCommand):
    @command()
    def setup(self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")) -> int:
        """Provision a worktree (allocate ports, DB name, run overlay steps)."""
        worktree = resolve_worktree(worktree_id)
        if worktree.state == Worktree.State.CREATED:
            worktree.provision()
            worktree.save()
        else:
            worktree.refresh_ports_if_needed()

        overlay = get_overlay()

        # Write .env.worktree to ticket directory + symlink into repo worktrees
        envfile = write_env_worktree(worktree)
        if envfile:
            self.stdout.write(f"  Written: {envfile}")

        # direnv: append overlay .envrc lines + allow
        wt_path = (worktree.extra or {}).get("worktree_path")
        if wt_path and Path(wt_path).is_dir():
            _append_envrc_lines(wt_path, overlay.get_envrc_lines(worktree))
            subprocess.run(  # noqa: S603
                ["direnv", "allow", wt_path],
                capture_output=True,
                check=False,
            )
            if (Path(wt_path) / ".pre-commit-config.yaml").is_file():
                self.stdout.write("  Running: prek install")
                subprocess.run(
                    ["prek", "install", "-f"],
                    cwd=wt_path,
                    capture_output=True,
                    check=False,
                )

        # Import database (DSLR/dump fallback chain) before running provision steps
        if overlay.get_db_import_strategy(worktree) is not None:
            self.stdout.write("  Running: db-import")
            env = {**os.environ, **overlay.get_env_extra(worktree)}
            env.pop("VIRTUAL_ENV", None)
            # Set env so pg tools can connect
            os.environ.update(env)
            if not overlay.db_import(worktree):
                self.stderr.write("  WARNING: DB import failed. Continuing with provision steps...")

        for step in overlay.get_provision_steps(worktree):
            self.stdout.write(f"  Running: {step.name}")
            step.callable()

        # Run post-DB steps (max_migration refresh, additional migrations, collectstatic)
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

        _write_skill_metadata_cache()

        return int(worktree.pk)

    @command()
    def start(self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")) -> str:
        worktree = resolve_worktree(worktree_id)
        commands = get_overlay().get_run_commands(worktree)
        worktree.start_services(services=list(commands))
        worktree.save()
        return worktree.state

    @command()
    def status(
        self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")
    ) -> dict[str, str]:
        worktree = resolve_worktree(worktree_id)
        return {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
        }

    @command()
    def teardown(self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")) -> str:
        worktree = resolve_worktree(worktree_id)
        worktree.teardown()
        worktree.save()
        return worktree.state

    @command()
    def clean(self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")) -> str:
        """Teardown worktree — stop services, drop DB, clean state."""
        worktree = resolve_worktree(worktree_id)
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
