"""The ``plugin-install-*`` staging dirs a failed or timed-out ``codex plugin add`` leaves behind."""

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def staging_dirs(home: Path) -> set[Path]:
    return {
        path
        for path in (home / "plugins" / "cache").glob("*/plugin-install-*")
        if path.is_dir() and not path.is_symlink()
    }


@contextmanager
def reclaiming_new_staging(home: Path) -> Iterator[None]:
    before = staging_dirs(home)
    try:
        yield
    finally:
        for leftover in sorted(staging_dirs(home) - before):
            try:
                shutil.rmtree(leftover)
            except OSError as exc:
                typer.echo(f"WARN  Could not remove Codex plugin staging {leftover}: {exc}", err=True)
            else:
                typer.echo(f"OK    Removed Codex plugin staging left by the add: {leftover}")
