"""Database operations: refresh, restore from CI, reset passwords."""

import os
import sys

import typer
from django_typer.management import TyperCommand, command

from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.utils.approval import ApprovalRefusedError, require_interactive_approval


class Command(TyperCommand):
    @command()
    def refresh(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        dslr_snapshot: str = typer.Option("", help="Force a specific DSLR snapshot name."),
        dump_path: str = typer.Option("", help="Path to a .pgsql dump file to restore from."),
        *,
        force: bool = False,
        fresh_dump: bool = typer.Option(
            default=False,
            help="Pull a fresh dump from the remote DEV environment for this tenant. "
            "Requires explicit interactive approval on every run.",
        ),
    ) -> str:
        """Re-import the worktree database from DSLR snapshot or dump.

        Without --force: tries DSLR restore first (fast), then full reimport.
        With --force: drops existing DB first, then reimports from scratch.
        Use --dslr-snapshot to force a specific snapshot (skip auto-discovery).
        Use --dump-path to restore from a specific dump file.
        Use --fresh-dump to pull a fresh dump from the remote DEV env — this
        is the only sanctioned remote-dump path and it requires an explicit
        per-invocation interactive approval (#777); an unattended agent
        cannot self-approve it.
        """
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        strategy = overlay.get_db_import_strategy(worktree)
        if strategy is None:
            return "No DB import strategy configured in the overlay."

        if fresh_dump:
            tenant = str(strategy.get("source_database", "")) or "<tenant>"
            prompt = (
                "FRESH REMOTE DUMP REQUESTED.\n"
                f"  Source environment : DEV (remote)\n"
                f"  Tenant / source DB : {tenant}\n"
                f"  Target worktree DB : {worktree.db_name}\n"
                "This pulls gigabytes over the network from the shared DEV database "
                "and overwrites the target DB. It must be explicitly approved every run."
            )
            try:
                require_interactive_approval(prompt, stdin=sys.stdin, stdout=sys.stdout)
            except ApprovalRefusedError as exc:
                return f"Fresh remote dump aborted: {exc}"

        self.stdout.write(f"Refreshing DB '{worktree.db_name}' (force={force})...")

        # Set overlay env vars so pg tools can connect with the right credentials
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        os.environ.update(overlay.get_env_extra(worktree))

        # Run the overlay's import logic
        success = overlay.db_import(
            worktree,
            force=force,
            dslr_snapshot=dslr_snapshot,
            dump_path=dump_path,
            approve_remote_dump=fresh_dump,
        )
        if not success:
            return f"DB import failed for {worktree.db_name}. Check output above for details."

        # Run post-DB steps (migrations, collectstatic, etc.)
        for step in overlay.get_post_db_steps(worktree):
            self.stdout.write(f"  Running post-DB step: {step.name}")
            step.callable()

        # Reset passwords
        reset_step = overlay.get_reset_passwords_command(worktree)
        if reset_step:  # pragma: no branch
            self.stdout.write("  Resetting passwords...")
            reset_step.callable()

        # FSM transition
        worktree.db_refresh()
        worktree.save()
        return f"DB refreshed for {worktree.db_name}"

    @command(name="restore-ci")
    def restore_ci(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Restore the worktree database from the latest CI dump."""
        worktree = resolve_worktree(path)
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
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Reset all user passwords to a known dev value."""
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        step = overlay.get_reset_passwords_command(worktree)
        if not step:
            return "No reset-passwords command configured in the overlay."
        step.callable()
        return f"Passwords reset for worktree {worktree.repo_path}"
