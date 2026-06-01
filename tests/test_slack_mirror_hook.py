"""Slack mirror for AskUserQuestion fires on PreToolUse, synchronously.

The mirror posts a DM to the user so they see the question on Slack
**before** they answer in the terminal. The previous detached/forked
implementation made the message land *after* the user had answered;
this is now a synchronous call with a per-user channel cache so the
post fits inside the hook timeout.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router

pytestmark = pytest.mark.django_db


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
            patch.object(router, "_perform_slack_post") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            mock_post.return_value = None
            result = router.handle_mirror_question_to_slack(self._question_payload())
        assert result is False

    def test_ignores_other_tools(self) -> None:
        with patch.object(router, "_perform_slack_post") as mock_post:
            router.handle_mirror_question_to_slack({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        mock_post.assert_not_called()

    def test_dispatches_synchronously_when_questions_present(self) -> None:
        with (
            patch.object(router, "_perform_slack_post") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            router.handle_mirror_question_to_slack(self._question_payload())
        mock_post.assert_called_once()
        slack_cfg, questions = mock_post.call_args.args
        assert slack_cfg == ("tok/ref", "U1")
        assert questions[0]["question"] == "Ship it?"

    def test_no_dispatch_when_no_questions(self) -> None:
        with patch.object(router, "_perform_slack_post") as mock_post:
            router.handle_mirror_question_to_slack({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}})
        mock_post.assert_not_called()

    def test_no_dispatch_when_slack_not_configured(self) -> None:
        with (
            patch.object(router, "_slack_config_from_toml", return_value=None),
            patch.object(router, "_perform_slack_post") as mock_post,
        ):
            router.handle_mirror_question_to_slack(self._question_payload())
        mock_post.assert_not_called()


class TestPresentModeMirrorsButDoesNotDeny:
    """Present-mode AskUserQuestion mirrors to Slack and is NOT denied (#182).

    In present mode the question still renders in the client; the mirror
    only ADDS a Slack DM so the user sees it on their phone too. The
    handler must never deny — denying would suppress the in-client prompt.
    """

    def _payload(self) -> dict:
        return {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}]}]},
        }

    def test_present_mode_posts_and_returns_false(self) -> None:
        with (
            patch.object(router, "_resolved_away_mode", return_value=False),
            patch.object(router, "_perform_slack_post") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            away_verdict = router.handle_route_away_mode_question(self._payload())
            mirror_verdict = router.handle_mirror_question_to_slack(self._payload())
        assert away_verdict is False
        assert mirror_verdict is False
        mock_post.assert_called_once()


class TestDmChannelCache:
    def test_round_trip_through_cache_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert router._read_dm_channel_cache("U1") == ""
        router._write_dm_channel_cache("U1", "D123")
        assert router._read_dm_channel_cache("U1") == "D123"

    def test_multiple_users_in_same_cache(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        router._write_dm_channel_cache("U1", "D1")
        router._write_dm_channel_cache("U2", "D2")
        assert router._read_dm_channel_cache("U1") == "D1"
        assert router._read_dm_channel_cache("U2") == "D2"

    def test_corrupt_cache_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        path = router._slack_dm_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert router._read_dm_channel_cache("U1") == ""


class TestSlackPostDm:
    def test_cache_hit_skips_open_dm(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        router._write_dm_channel_cache("U1", "D-cached")
        with (
            patch.object(router, "_slack_open_dm") as mock_open,
            patch.object(router, "_slack_post_message", return_value=True) as mock_post,
        ):
            router._slack_post_dm("xoxb-tok", "U1", "hello")
        mock_open.assert_not_called()
        mock_post.assert_called_once_with("xoxb-tok", "D-cached", "hello", timeout=2.0)

    def test_cache_miss_opens_dm_and_caches(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with (
            patch.object(router, "_slack_open_dm", return_value="D-new") as mock_open,
            patch.object(router, "_slack_post_message", return_value=True) as mock_post,
        ):
            router._slack_post_dm("xoxb-tok", "U1", "hello")
        mock_open.assert_called_once()
        mock_post.assert_called_once_with("xoxb-tok", "D-new", "hello", timeout=2.0)
        assert router._read_dm_channel_cache("U1") == "D-new"

    def test_stale_cache_falls_back_to_open(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        router._write_dm_channel_cache("U1", "D-stale")
        with (
            patch.object(router, "_slack_open_dm", return_value="D-fresh") as mock_open,
            patch.object(router, "_slack_post_message", side_effect=[False, True]) as mock_post,
        ):
            router._slack_post_dm("xoxb-tok", "U1", "hello")
        mock_open.assert_called_once()
        assert mock_post.call_count == 2
        assert router._read_dm_channel_cache("U1") == "D-fresh"


class TestHooksJsonWiring:
    def test_askuserquestion_matcher_lives_on_pretooluse(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        pre_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PreToolUse", [])]
        post_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PostToolUse", [])]
        assert "AskUserQuestion" in pre_matchers
        assert "AskUserQuestion" not in post_matchers

    def test_askuserquestion_hook_timeout_allows_sync_post(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        ask_entry = next(
            entry for entry in hooks_config["hooks"]["PreToolUse"] if entry.get("matcher") == "AskUserQuestion"
        )
        # Synchronous post needs to fit pass-show + (cache hit) chat.postMessage
        # under the timeout. 3s was too tight; 5s+ keeps a safety margin.
        assert ask_entry["hooks"][0]["timeout"] >= 5
