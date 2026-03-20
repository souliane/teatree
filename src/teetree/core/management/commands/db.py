"""Database operations: refresh, restore from CI, reset passwords."""

import os
import subprocess  # noqa: S404

import typer
from django_typer.management import TyperCommand, command

from teetree.core.overlay_loader import get_overlay
from teetree.core.resolve import resolve_worktree


class Command(TyperCommand):
    @command()
    def refresh(
        self,
        worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)"),
        *,
        force: bool = False,
    ) -> str:
        """Re-import the worktree database from DSLR snapshot or dump.

        Without --force: tries DSLR restore first (fast), then full reimport.
        With --force: drops existing DB first, then reimports from scratch.
        """
        worktree = resolve_worktree(worktree_id)
        overlay = get_overlay()
        strategy = overlay.get_db_import_strategy(worktree)
        if strategy is None:
            return "No DB import strategy configured in the overlay."

        self.stdout.write(f"Refreshing DB '{worktree.db_name}' (force={force})...")

        # Set overlay env vars so pg tools can connect with the right credentials
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        os.environ.update(overlay.get_env_extra(worktree))

        # Run the overlay's import logic
        success = overlay.db_import(worktree, force=force)
        if not success:
            return f"DB import failed for {worktree.db_name}. Check output above for details."

        # Run post-DB steps (migrations, collectstatic, etc.)
        for step in overlay.get_post_db_steps(worktree):
            self.stdout.write(f"  Running post-DB step: {step.get('name', '?')}")
            cmd = step.get("command", "")
            if cmd:
                subprocess.run(cmd, shell=True, check=False, env=env)  # noqa: S602

        # Reset passwords
        reset_cmd = overlay.get_reset_passwords_command(worktree)
        if reset_cmd:  # pragma: no branch
            self.stdout.write("  Resetting passwords...")
            subprocess.run(reset_cmd, shell=True, check=False, env=env)  # noqa: S602

        # FSM transition
        worktree.db_refresh()
        worktree.save()
        return f"DB refreshed for {worktree.db_name}"

    @command(name="restore-ci")
    def restore_ci(self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")) -> str:
        """Restore the worktree database from the latest CI dump."""
        worktree = resolve_worktree(worktree_id)
        overlay = get_overlay()
        strategy = overlay.get_db_import_strategy(worktree)
        if strategy is None:
            return "No DB import strategy configured in the overlay."

        # Use db_import with a hint to skip DSLR/local and go straight to CI
        success = overlay.db_import(worktree, force=True)
        if not success:
            return f"CI restore failed for {worktree.db_name}."
        worktree.db_refresh()
        worktree.save()
        return f"DB restored from CI for {worktree.db_name}"

    @command(name="reset-passwords")
    def reset_passwords(
        self, worktree_id: int = typer.Argument(0, help="Worktree ID (auto-detects from PWD if 0)")
    ) -> str:
        """Reset all user passwords to a known dev value."""
        worktree = resolve_worktree(worktree_id)
        overlay = get_overlay()
        cmd = overlay.get_reset_passwords_command(worktree)
        if not cmd:
            return "No reset-passwords command configured in the overlay."
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        subprocess.run(cmd, shell=True, check=True, env=env)  # noqa: S602
        return f"Passwords reset for worktree {worktree.repo_path}"
