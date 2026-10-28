"""Distinguish direct owner commands from agent-issued CLI calls."""

import os
import sys

from teatree.core.session_identity import session_id_from_env
from teatree.utils.agent_spawn_marker import is_dispatched_agent_process


def direct_owner_action() -> bool:
    return (
        sys.stdin.isatty()
        and not is_dispatched_agent_process()
        and session_id_from_env() is None
        and not os.environ.get("CODEX_THREAD_ID")
        and not os.environ.get("CLAUDECODE")
    )
