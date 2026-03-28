import os
import sys
from pathlib import Path


def find_python(cwd: str | Path = ".") -> str:
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidate = Path(venv) / "bin" / "python"
        if candidate.is_file():
            return str(candidate)

    local = Path(cwd) / ".venv" / "bin" / "python"
    if local.is_file():
        return str(local)

    return sys.executable


def find_activate(cwd: str | Path = ".") -> str:
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidate = Path(venv) / "bin" / "activate"
        if candidate.is_file():
            return str(candidate)

    local = Path(cwd) / ".venv" / "bin" / "activate"
    if local.is_file():
        return str(local)

    return ""
