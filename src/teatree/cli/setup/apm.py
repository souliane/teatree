"""APM dependency install + apm-hook stripping for ``t3 setup``."""

import json
import shutil
from pathlib import Path

import typer

from teatree.cli.setup._process import run_captured
from teatree.utils.run import CompletedProcess

_APM_FAILURE_MARKERS = ("installation failed", "package failed", "package(s) failed")


def _apm_reported_failure(result: CompletedProcess[str]) -> bool:
    """Whether an apm-install run failed, accounting for apm's exit-0-on-error.

    apm writes its per-package diagnostics to *stdout* and (in some versions)
    exits 0 even when a package fails validation, so a bare returncode check
    misses the failure entirely.  Treat a non-zero exit OR a failure marker in
    either stream as failure.
    """
    if result.returncode != 0:
        return True
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in combined for marker in _APM_FAILURE_MARKERS)


class ApmInstaller:
    """Run ``apm install -g --target claude`` from the teatree repo."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def install(self) -> bool:
        apm_path = shutil.which("apm")
        if not apm_path:
            typer.echo("WARN  apm not found — skipping APM dependency installation.")
            typer.echo("      Install: pip install apm-cli (or brew install microsoft/apm/apm)")
            return False

        result = run_captured([apm_path, "install", "-g", "--target", "claude"], cwd=self.repo)
        if _apm_reported_failure(result):
            detail = result.stdout.strip() or result.stderr.strip() or "(no output)"
            typer.echo(f"WARN  apm install failed: {detail}")
            return False
        typer.echo("OK    APM dependencies installed globally.")
        return True


def strip_apm_hooks(settings_path: Path) -> int:
    """Remove hook entries injected by APM (_apm_source key) from settings.json."""
    if not settings_path.is_file():
        return 0

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    keys_to_check = list(hooks.keys())
    for key in keys_to_check:
        entries = hooks[key]
        if isinstance(entries, list):
            original_len = len(entries)
            hooks[key] = [e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)]
            removed += original_len - len(hooks[key])
            if not hooks[key]:
                del hooks[key]

    if not hooks and "hooks" in data:
        del data["hooks"]

    if removed > 0:
        settings_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")

    return removed
