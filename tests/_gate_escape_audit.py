"""Answer a hook's ``t3 tool gate-escape`` audit at the subprocess boundary, recording each request."""

import contextlib
import json
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

import hooks.scripts.hook_router as router
from hooks.scripts import gate_escape
from teatree.cli import app

_ARGV = ["/usr/local/bin/t3", "tool", "gate-escape"]
type Budget = Callable[[float], float | None]


@contextlib.contextmanager
def audited_escape(
    data: dict, *, recorded: bool = True, via_cli: bool = False, event: str = "PreToolUse"
) -> Generator[list[dict]]:
    """Run *data* as the live call; ``via_cli`` writes the audit row through the real command."""
    requests: list[dict] = []

    def answer(argv: list[str], *, timeout: float, stdin_text: str, budget: Budget) -> subprocess.CompletedProcess[str]:
        requests.append(json.loads(stdin_text))
        if via_cli:
            result = CliRunner().invoke(app, argv[1:], input=stdin_text)
            return subprocess.CompletedProcess(argv, result.exit_code, result.stdout, "")
        return subprocess.CompletedProcess(argv, 0 if recorded else 1, json.dumps({"allowed": recorded}), "")

    data.setdefault("cwd", "/work/repo")
    with (
        patch.object(router, "_CURRENT_EVENT", event),
        patch.object(router, "_CURRENT_DATA", data),
        patch.object(gate_escape, "t3_argv", return_value=_ARGV),
        patch.object(gate_escape, "_head", return_value="a" * 40),
        patch.object(gate_escape, "run_t3", answer),
    ):
        yield requests


def write_recording_t3(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "t3"
    script.write_text(
        '#!/bin/sh\ncat > "$(dirname "$0")/gate-escape-request.json"\necho \'{"allowed": true}\'\n', encoding="utf-8"
    )
    script.chmod(0o755)
    return bin_dir / "gate-escape-request.json"
