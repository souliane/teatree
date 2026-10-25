# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""`deploy/t3 … secrets get` refuses a host terminal, because `compose exec -T` hides it from the container."""

import os
import pty
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_BASH = shutil.which("bash") or "bash"
FUNCTION = "refuse_secrets_get_on_terminal"


def _wrapper_function() -> str:
    lines = WRAPPER.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{FUNCTION}() {{")
    return "\n".join(lines[start : lines.index("}", start) + 1])


def _run(*args: str, terminal: bool) -> tuple[int, str, str]:
    script = f'set -euo pipefail\n{_wrapper_function()}\n{FUNCTION} "$@"\necho reached\n'
    argv = [_BASH, "-c", script, "t3", *args]
    if terminal:
        master, slave = pty.openpty()
        completed = subprocess.run(argv, stdout=slave, stderr=subprocess.PIPE, text=True, check=False, timeout=60)
        os.close(slave)
        try:
            out = os.read(master, 4096).decode()
        except OSError:
            out = ""
        os.close(master)
        return completed.returncode, out, completed.stderr
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    return completed.returncode, completed.stdout, completed.stderr


def test_secrets_get_on_a_host_terminal_is_refused() -> None:
    code, out, err = _run("teatree", "secrets", "get", "team/token", terminal=True)
    assert code == 3
    assert "reached" not in out
    assert "terminal" in err


def test_secrets_get_through_a_pipe_proceeds() -> None:
    code, out, _ = _run("teatree", "secrets", "get", "team/token", terminal=False)
    assert (code, out.strip()) == (0, "reached")


def test_unrelated_command_containing_secrets_get_words_is_not_refused() -> None:
    code, out, _ = _run("teatree", "ticket", "comment", "secrets", "get", terminal=True)
    assert code == 0
    assert "reached" in out


@pytest.mark.parametrize(
    "args", [("teatree", "secrets", "status"), ("teatree", "doctor"), ("teatree", "secrets", "exec")]
)
def test_other_commands_on_a_terminal_proceed(args: tuple[str, ...]) -> None:
    code, out, _ = _run(*args, terminal=True)
    assert code == 0
    assert "reached" in out
