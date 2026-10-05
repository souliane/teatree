"""The script a shell wrapper runs is read out, never evaluated."""

import pytest

from teatree.agents.shell_wrapper import unwrap_lone_shell
from tests.teatree_agents import _codex_command_shape as _codex_shape


@pytest.mark.parametrize(
    ("command", "script"),
    [
        (_codex_shape.codex_wrapped("git add -A"), "git add -A"),
        (_codex_shape.codex_wrapped("cd /work && git push"), "cd /work && git push"),
        ("bash -c 'pwd'", "pwd"),
        ("env FOO=1 /bin/sh -c 'pwd'", "pwd"),
        ("/bin/ksh -lc 'pwd'", "pwd"),
        ("git add -A", "git add -A"),
        ("cd /work && bash -lc 'git push'", "cd /work && bash -lc 'git push'"),
        ("bash ./script.sh", "bash ./script.sh"),
        ("cat 'unterminated", "cat 'unterminated"),
    ],
)
def test_a_lone_shell_wrapper_yields_its_script_and_anything_else_is_left_whole(command: str, script: str) -> None:
    assert unwrap_lone_shell(command) == script
