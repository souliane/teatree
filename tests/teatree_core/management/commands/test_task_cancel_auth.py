from unittest.mock import patch

from teatree.core.management.commands.task_cancel_auth import direct_owner_cancel


def test_only_a_direct_terminal_call_counts_as_owner_cancel() -> None:
    with (
        patch("teatree.core.management.commands.task_cancel_auth.sys.stdin.isatty", return_value=True),
        patch("teatree.core.management.commands.task_cancel_auth.is_dispatched_agent_process", return_value=False),
        patch("teatree.core.management.commands.task_cancel_auth.session_id_from_env", return_value=None),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert direct_owner_cancel()


def test_dispatched_or_interactive_agent_never_counts_as_owner() -> None:
    with (
        patch("teatree.core.management.commands.task_cancel_auth.sys.stdin.isatty", return_value=True),
        patch("teatree.core.management.commands.task_cancel_auth.is_dispatched_agent_process", return_value=True),
    ):
        assert not direct_owner_cancel()
    with (
        patch("teatree.core.management.commands.task_cancel_auth.sys.stdin.isatty", return_value=True),
        patch("teatree.core.management.commands.task_cancel_auth.is_dispatched_agent_process", return_value=False),
        patch("teatree.core.management.commands.task_cancel_auth.session_id_from_env", return_value="session"),
    ):
        assert not direct_owner_cancel()


def test_codex_or_claude_terminal_agent_never_counts_as_owner() -> None:
    with (
        patch("teatree.core.management.commands.task_cancel_auth.sys.stdin.isatty", return_value=True),
        patch("teatree.core.management.commands.task_cancel_auth.is_dispatched_agent_process", return_value=False),
        patch("teatree.core.management.commands.task_cancel_auth.session_id_from_env", return_value=None),
        patch.dict("os.environ", {"CODEX_THREAD_ID": "agent"}),
    ):
        assert not direct_owner_cancel()
    with (
        patch("teatree.core.management.commands.task_cancel_auth.sys.stdin.isatty", return_value=True),
        patch("teatree.core.management.commands.task_cancel_auth.is_dispatched_agent_process", return_value=False),
        patch("teatree.core.management.commands.task_cancel_auth.session_id_from_env", return_value=None),
        patch.dict("os.environ", {"CLAUDECODE": "1"}),
    ):
        assert not direct_owner_cancel()
