"""Global ``t3`` install / editable-repair for ``t3 setup``."""

import shutil
from pathlib import Path

import typer

from teatree.cli.setup._process import run_captured
from teatree.self_update import current_editable_source


class ToolInstaller:
    """Ensure a healthy global ``t3`` install anchored at the main clone."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def ensure_installed(self) -> bool:
        """Ensure a healthy global ``t3`` via ``uv tool install``.

        Steady-state: if ``t3`` is on PATH and the editable source recorded by uv
        still exists, leave the install alone.  This preserves intentional
        worktree-dogfood installs (see #397 Part 3) and non-editable installs.

        Repair path: when the editable source has been deleted (e.g. the worktree
        it was installed from got cleaned up), reinstall editable from the main
        clone so the global ``t3`` is re-anchored at a stable path.
        """
        uv_bin = shutil.which("uv")
        t3_on_path = shutil.which("t3") is not None

        if t3_on_path and uv_bin:
            source = current_editable_source(uv_bin)
            if source is None or source.is_dir():
                return True
            typer.echo(f"NOTE  Global `t3` editable source missing: {source}")
            typer.echo(f"      Re-anchoring at main clone {self.repo}.")
        elif t3_on_path:
            return True

        if not uv_bin:
            typer.echo("WARN  `t3` not on PATH and `uv` is missing — skipping global install.")
            typer.echo("      Install uv: https://docs.astral.sh/uv/getting-started/installation/")
            return False

        result = run_captured([uv_bin, "tool", "install", "--force", "--editable", str(self.repo)])
        if result.returncode != 0:
            typer.echo(f"WARN  `uv tool install` failed: {result.stderr.strip()}")
            return False
        typer.echo("OK    Installed `t3` globally via `uv tool install --editable`.")
        if not shutil.which("t3"):
            self._print_path_hint(self._uv_tool_bin_dir(uv_bin))
        return True

    @staticmethod
    def _uv_tool_bin_dir(uv_bin: str) -> Path | None:
        """Return the directory ``uv tool`` installs binaries into, or None on error."""
        result = run_captured([uv_bin, "tool", "dir", "--bin"])
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return Path(result.stdout.strip()).expanduser()

    @staticmethod
    def _print_path_hint(bin_dir: Path | None) -> None:
        """Print a shell-rc instruction when the uv tool bin dir is not on PATH."""
        target = bin_dir or Path.home() / ".local" / "bin"
        typer.echo(f"NOTE  `{target}` is not on your PATH.")
        typer.echo(f'      Add to your shell rc (~/.zshrc or ~/.bashrc): export PATH="{target}:$PATH"')
