"""Slack mirror for AskUserQuestion fires on PreToolUse, non-blocking."""

import json
from pathlib import Path
from unittest.mock import patch

import hooks.scripts.hook_router as router


class TestRouterRegistration:
    def test_mirror_handler_is_registered_under_pretooluse(self) -> None:
        assert router.handle_mirror_question_to_slack in router._HANDLERS["PreToolUse"]

    def test_mirror_handler_is_not_registered_under_posttooluse(self) -> None:
        assert router.handle_mirror_question_to_slack not in router._HANDLERS["PostToolUse"]


class TestMirrorHandler:
    def _question_payload(self) -> dict:
        return {
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {
                        "question": "Ship it?",
                        "options": [
                            {"label": "Yes", "description": "go"},
                            {"label": "No", "description": "wait"},
                        ],
                    }
                ]
            },
        }

    def test_returns_false_so_chain_continues(self) -> None:
        with (
            patch.object(router, "_dispatch_slack_post_detached") as mock_dispatch,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            mock_dispatch.return_value = None
            result = router.handle_mirror_question_to_slack(self._question_payload())
        assert result is False

    def test_ignores_other_tools(self) -> None:
        with patch.object(router, "_dispatch_slack_post_detached") as mock_dispatch:
            router.handle_mirror_question_to_slack({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        mock_dispatch.assert_not_called()

    def test_dispatches_when_questions_present_and_slack_configured(self) -> None:
        with (
            patch.object(router, "_dispatch_slack_post_detached") as mock_dispatch,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            router.handle_mirror_question_to_slack(self._question_payload())
        mock_dispatch.assert_called_once()
        slack_cfg, questions = mock_dispatch.call_args.args
        assert slack_cfg == ("tok/ref", "U1")
        assert questions[0]["question"] == "Ship it?"

    def test_no_dispatch_when_no_questions(self) -> None:
        with patch.object(router, "_dispatch_slack_post_detached") as mock_dispatch:
            router.handle_mirror_question_to_slack({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}})
        mock_dispatch.assert_not_called()

    def test_no_dispatch_when_slack_not_configured(self) -> None:
        with (
            patch.object(router, "_slack_config_from_toml", return_value=None),
            patch.object(router, "_dispatch_slack_post_detached") as mock_dispatch,
        ):
            router.handle_mirror_question_to_slack(self._question_payload())
        mock_dispatch.assert_not_called()


class TestHooksJsonWiring:
    def test_askuserquestion_matcher_lives_on_pretooluse(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        pre_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PreToolUse", [])]
        post_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PostToolUse", [])]
        assert "AskUserQuestion" in pre_matchers
        assert "AskUserQuestion" not in post_matchers
