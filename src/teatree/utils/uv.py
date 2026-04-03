"""Helpers for running Python tools via ``uv``."""

import shutil


def uv_cmd(tool: str) -> list[str]:
    """Return command prefix for running a Python tool via ``uv run``.

    Always prefers ``uv run`` over bare executables to avoid pyenv shims
    and ensure the correct project venv is used.  Raises ``RuntimeError``
    if ``uv`` is not installed.
    """
    if not shutil.which("uv"):
        msg = f"uv is required to run '{tool}' but is not installed"
        raise RuntimeError(msg)
    return ["uv", "run", tool]
