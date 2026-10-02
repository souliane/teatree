"""Shared subprocess helper for the setup units."""

from pathlib import Path

from teatree.utils.run import CompletedProcess, run_allowed_to_fail


def run_captured(args: list[str], cwd: Path | None = None) -> CompletedProcess[str]:
    """Run a subprocess, capturing stdout/stderr and never raising on non-zero exit."""
    return run_allowed_to_fail(args, cwd=cwd, expected_codes=None)
