"""The env marker the Claude and Codex spawn seams set, so every process a dispatched agent runs carries it."""

import os

DISPATCHED_AGENT_ENV = "T3_DISPATCHED_AGENT"


def dispatched_agent_env() -> dict[str, str]:
    return {DISPATCHED_AGENT_ENV: "1"}


def is_dispatched_agent_process() -> bool:
    return bool(os.environ.get(DISPATCHED_AGENT_ENV, "").strip())
