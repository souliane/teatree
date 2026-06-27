"""``t3 teatree env`` — inspect and mutate worktree env via the DB.

The env cache on disk is a derived artifact.  These subcommands read
from / write to the authoritative source (Django models + overlay
config), never the cache file.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from django.core.management import execute_from_command_line
from django_typer.management import TyperCommand, command

from teatree.core.models import Worktree, WorktreeEnvOverride
from teatree.core.resolve import resolve_worktree
from teatree.core.worktree_env import (
    CACHE_DIRNAME,
    CACHE_FILENAME,
    detect_drift,
    load_overrides,
    render_env_cache,
    set_override,
    write_env_cache,
)
from teatree.utils.postgres_secret import (
    PostgresPasswordUnavailableError,
    ensure_postgres_pass_entry,
    extract_literal_from_cache,
)


class Command(TyperCommand):
    @command()
    def show(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        output_format: str = typer.Option("shell", "--format", help="shell | json"),
    ) -> int:
        """Print the current env as the DB would render it.

        Never reads the cache file — always renders fresh from the DB.
        """
        worktree = resolve_worktree(path)
        spec = render_env_cache(worktree)
        if spec is None:
            self.stderr.write(f"  {worktree.repo_path}: no worktree_path — not provisioned.")
            return 1

        pairs = {}
        for line in spec.content.splitlines():
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            pairs[key] = value

        if output_format == "json":
            self.stdout.write(json.dumps(pairs, indent=2, sort_keys=False))
        else:
            for key, value in pairs.items():
                self.stdout.write(f"{key}={value}")
        return 0

    @command()
    def set_var(
        self,
        key_value: str = typer.Argument(..., help="KEY=VALUE."),
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> int:
        """Persist an override on the worktree and refresh the cache.

        Rejects keys owned by core (edit the model field instead).
        """
        if "=" not in key_value:
            self.stderr.write("  expected KEY=VALUE")
            return 2
        key, _, value = key_value.partition("=")
        worktree = resolve_worktree(path)
        try:
            set_override(worktree, key, value)
        except ValueError as exc:
            self.stderr.write(f"  {exc}")
            return 1
        self.stdout.write(f"  set {key} on {worktree.repo_path}")
        return 0

    @command()
    def unset(
        self,
        key: str = typer.Argument(..., help="Override key to remove."),
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> int:
        """Delete an override row and refresh the cache."""
        worktree = resolve_worktree(path)
        deleted, _ = WorktreeEnvOverride.objects.filter(worktree=worktree, key=key).delete()
        if deleted:
            write_env_cache(worktree)
            self.stdout.write(f"  removed {key} from {worktree.repo_path}")
            return 0
        self.stderr.write(f"  no override named {key} on {worktree.repo_path}")
        return 1

    @command()
    def overrides(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> int:
        """List user-declared overrides for this worktree."""
        worktree = resolve_worktree(path)
        rows = load_overrides(worktree)
        if not rows:
            self.stdout.write("  (no overrides)")
            return 0
        for key, value in sorted(rows.items()):
            self.stdout.write(f"  {key}={value}")
        return 0

    @command(name="check")
    def check_drift(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> int:
        """Exit non-zero if the on-disk cache diverges from the DB render.

        The Python method is named ``check_drift`` (not ``check``) to avoid
        shadowing :meth:`django.core.management.base.BaseCommand.check`,
        which Django invokes on every command to run the system-checks
        framework. The typer subcommand name is still ``check``.
        """
        worktree = resolve_worktree(path)
        drifted, cache_path = detect_drift(worktree)
        if drifted:
            self.stderr.write(
                f"  env cache stale at {cache_path} — rerun `t3 <overlay> worktree start`",
            )
            return 1
        self.stdout.write(f"  {worktree.repo_path}: env cache in sync with DB")
        return 0

    @command(name="migrate-secrets")
    def migrate_secrets(
        self,
        path: str = typer.Option(
            "",
            help="Worktree path (migrates only this worktree). Empty = migrate every worktree.",
        ),
    ) -> int:
        """Move ``POSTGRES_PASSWORD`` literals out of ``.t3-env.cache`` into ``pass``.

        For each targeted worktree this command:

        1. Reads the literal ``POSTGRES_PASSWORD=`` line from the on-disk cache.
        2. Stores it in ``pass`` under the canonical key for that worktree.
        3. Regenerates the cache so it now contains only the symbolic
            ``POSTGRES_PASSWORD_PASS_KEY`` reference.

        Idempotent — caches that already lack a literal are reported as
        ``already migrated`` and left alone.  Exits 0 when every targeted
        worktree finished successfully, non-zero when at least one needs
        attention (no pass installed, cache missing, etc.).
        """
        targets = [resolve_worktree(path)] if path else list(Worktree.objects.all())
        if not targets:
            self.stdout.write("  no worktrees found — nothing to migrate")
            return 0

        failures = 0
        for worktree in targets:
            outcome = self._migrate_single_worktree(worktree)
            self.stdout.write(f"  {worktree.repo_path}: {outcome.message}")
            if not outcome.ok:
                failures += 1
        return 0 if failures == 0 else 1

    def _migrate_single_worktree(self, worktree: Worktree) -> "_MigrationOutcome":
        cache_path = _cache_path_for(worktree)
        if cache_path is None:
            return _MigrationOutcome(ok=True, message="not provisioned — skipped")

        literal = extract_literal_from_cache(cache_path)
        ticket = worktree.ticket
        if ticket is None:
            return _MigrationOutcome(ok=False, message="no ticket attached — cannot derive pass key")

        if not literal:
            write_env_cache(worktree)
            return _MigrationOutcome(ok=True, message="already migrated (no literal in cache)")

        try:
            # Keyed on the ticket pk (the canonical, unique key), matching
            # ``Worktree.pass_key`` the env render writes into the cache.
            pass_key = ensure_postgres_pass_entry(ticket.pk, literal)
        except PostgresPasswordUnavailableError as exc:
            return _MigrationOutcome(ok=False, message=str(exc))

        # Regenerate the cache from the DB so the literal is replaced with the
        # symbolic reference.  The render layer already drops POSTGRES_PASSWORD.
        write_env_cache(worktree)
        return _MigrationOutcome(ok=True, message=f"migrated to pass key {pass_key}")


def _cache_path_for(worktree: Worktree) -> Path | None:
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    if not wt_path:
        return None
    return Path(wt_path).parent / CACHE_DIRNAME / CACHE_FILENAME


@dataclass(frozen=True, slots=True)
class _MigrationOutcome:
    """Outcome of migrating one worktree — used by ``migrate-secrets``.

    Replaces ad-hoc tuples so each line of code reads ``outcome.ok`` and
    ``outcome.message`` instead of indexed access.
    """

    ok: bool
    message: str


def main() -> int:  # pragma: no cover — module entry point (Django dispatch glue)
    execute_from_command_line([sys.argv[0], "env", *sys.argv[1:]])
    return 0
