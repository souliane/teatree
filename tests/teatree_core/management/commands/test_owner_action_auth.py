from unittest.mock import patch

import pytest

from teatree.core.management.commands.owner_action_auth import direct_owner_action
from teatree.core.session_identity import SESSION_ID_ENV_VARS
from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV


def _owner_terminal(env: dict[str, str], *, tty: bool = True) -> bool:
    with (
        patch("teatree.core.management.commands.owner_action_auth.sys.stdin.isatty", return_value=tty),
        patch.dict("os.environ", env, clear=True),
    ):
        return direct_owner_action()


def test_only_a_direct_terminal_call_counts_as_owner_action() -> None:
    assert _owner_terminal({})
    assert not _owner_terminal({}, tty=False)


@pytest.mark.parametrize(
    "agent_env",
    [
        {DISPATCHED_AGENT_ENV: "1"},
        {SESSION_ID_ENV_VARS[0]: "session"},
        {"CODEX_THREAD_ID": "agent"},
        {"CLAUDECODE": "1"},
    ],
)
def test_an_agent_terminal_never_counts_as_owner(agent_env: dict[str, str]) -> None:
    assert not _owner_terminal(agent_env)
