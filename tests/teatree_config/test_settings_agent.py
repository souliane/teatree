"""Agent declaration group remains a flat dataclass base."""

from teatree.config.settings_agent import _ModeHarnessSettings


def test_agent_group_defaults() -> None:
    settings = _ModeHarnessSettings()
    assert settings.agent_harness == "claude_sdk"
    assert settings.agent_max_turns == 3000
    assert settings.pydantic_ai_request_limit == 3000
    assert settings.watchdog_max_runtime_seconds == 86400
