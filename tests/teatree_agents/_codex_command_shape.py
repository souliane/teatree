"""The shell wrapper the pinned codex puts around every command it runs, as captured live."""

import json
import shlex
from pathlib import Path

_CAPTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "codex_app_server" / "0.155.1-command-shape.json").read_text(
        encoding="utf-8"
    )
)
CAPTURED_COMMAND: str = _CAPTURE["command"]
CAPTURED_INNER: str = _CAPTURE["inner_command"]
_WRAPPER = CAPTURED_COMMAND.removesuffix(shlex.quote(CAPTURED_INNER)).rstrip()


def codex_wrapped(command: str) -> str:
    """*command* as codex reports it: ``/bin/<shell> -lc '<command>'``."""
    return f"{_WRAPPER} {shlex.quote(command)}"
