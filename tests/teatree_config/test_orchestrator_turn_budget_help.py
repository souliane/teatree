"""The orchestrator turn budget describes its actual soft tool-call nudge."""

from teatree.config.setting_help import setting_help


def test_orchestrator_turn_budget_help_names_tool_calls_and_nudge() -> None:
    help_text = setting_help("orchestrator_turn_budget")
    assert "tool-call count" in help_text
    assert "nudges" in help_text
    assert "output-token" not in help_text
