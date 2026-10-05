"""``t3 teatree env`` — inspect and mutate worktree env via the DB.

The env cache on disk is a derived artifact.  These subcommands read
from / write to the authoritative source (Django models + overlay
config), never the cache file.
"""

import json
import sys

import typer
from django.core.management import execute_from_command_line
from django_typer.management import TyperCommand, command

from teatree.core.intake.resolve import resolve_worktree
from teatree.core.models import WorktreeEnvOverride
from teatree.core.worktree.worktree_env import (
    detect_drift,
    load_overrides,
    render_env_cache,
    set_override,
    write_env_cache,
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
            raise SystemExit(1)

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
            raise SystemExit(2)
        key, _, value = key_value.partition("=")
        worktree = resolve_worktree(path)
        try:
            set_override(worktree, key, value)
        except ValueError as exc:
            self.stderr.write(f"  {exc}")
            raise SystemExit(1) from None
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
        raise SystemExit(1)

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
            # django-typer prints a returned int; only SystemExit reaches the
            # shell, so returning 1 here reported a stale cache as in sync.
            self.stderr.write(
                f"  env cache stale at {cache_path} — rerun `t3 <overlay> worktree start`",
            )
            raise SystemExit(1)
        self.stdout.write(f"  {worktree.repo_path}: env cache in sync with DB")
        return 0


def main() -> int:  # pragma: no cover — module entry point (Django dispatch glue)
    execute_from_command_line([sys.argv[0], "env", *sys.argv[1:]])
    return 0
