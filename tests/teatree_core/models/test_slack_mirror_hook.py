"""Slack mirror for AskUserQuestion fires on PreToolUse, synchronously.

The mirror posts a DM to the user so they see the question on Slack
**before** they answer in the terminal. The previous detached/forked
implementation made the message land *after* the user had answered;
this is now a synchronous call with a per-user channel cache so the
post fits inside the hook timeout.
"""

import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
import hooks.scripts.slack_mirror_wiring as wiring
from teatree.core.models.deferred_question import DeferredQuestion


class _CapturedStdoutTestCase(TestCase):
    """A ``capsys``-shaped handle on what the hook printed, plus a scratch dir."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)

        self._stdout = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self._stdout))

    def drain_stdout(self) -> str:
        """Return everything printed since the last drain, then clear the buffer."""
        printed = self._stdout.getvalue()
        self._stdout.seek(0)
        self._stdout.truncate(0)
        return printed


class TestRouterRegistration(TestCase):
    def test_mirror_handler_is_registered_under_pretooluse(self) -> None:
        assert router.handle_mirror_question_to_slack in router._HANDLERS["PreToolUse"]

    def test_mirror_handler_is_not_registered_under_posttooluse(self) -> None:
        assert router.handle_mirror_question_to_slack not in router._HANDLERS["PostToolUse"]


class TestMirrorHandler(TestCase):
    """The mirror-without-deny dispatch path (the live-user-turn arm).

    These exercise the Slack mirror dispatch mechanics; they run under a
    live-user-turn so the handler mirrors and returns ``False`` (the
    loop-driven deny arm has its own class below).
    """

    def setUp(self) -> None:
        live_turn = patch.object(router, "_is_live_user_turn", lambda _data: True)
        live_turn.start()
        self.addCleanup(live_turn.stop)

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
            patch.object(router, "_perform_slack_post", return_value="1700.0001") as mock_post,
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


class TestPresentModeMirrorsButDoesNotDeny(TestCase):
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


class TestInteractiveQuestionReachesTheOwnerQueue(TestCase):
    """#3642 — an interactive question is answerable from Slack, like the headless lane.

    The live-user-turn arm must still render in-client (never deny), but the question
    also enters the shared owner-thread queue with its Slack coordinates bound, so a
    reply from Slack resolves it and the owner cannot lose it in an unwatched terminal.
    """

    def setUp(self) -> None:
        live_turn = patch.object(router, "_is_live_user_turn", lambda _data: True)
        live_turn.start()
        self.addCleanup(live_turn.stop)

    def _payload(self) -> dict:
        return {
            "tool_name": "AskUserQuestion",
            "session_id": "sess-interactive",
            "tool_use_id": "tu-1",
            "tool_input": {"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}]}]},
        }

    def test_the_question_is_recorded_and_mirror_linked_without_denying(self) -> None:
        with (
            patch.object(router, "_perform_slack_post", return_value="1779990001.000001"),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
            patch.object(router, "_read_dm_channel_cache", return_value="D0OWNER"),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload())

        assert verdict is False
        row = DeferredQuestion.objects.get()
        assert row.question == "Ship it?"
        assert row.slack_ts == "1779990001.000001"
        assert row.slack_channel == "D0OWNER"

    def test_a_slack_reply_can_resolve_the_interactive_question(self) -> None:
        with (
            patch.object(router, "_perform_slack_post", return_value="1779990001.000001"),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
            patch.object(router, "_read_dm_channel_cache", return_value="D0OWNER"),
        ):
            router.handle_mirror_question_to_slack(self._payload())

        live = DeferredQuestion.live_for_reply(channel="D0OWNER", after_ts="1779990002.000001")
        assert live is not None
        assert live.question == "Ship it?"

    def test_an_in_client_answer_resolves_the_row_so_slack_cannot_double_apply(self) -> None:
        with (
            patch.object(router, "_perform_slack_post", return_value="1779990001.000001"),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
            patch.object(router, "_read_dm_channel_cache", return_value="D0OWNER"),
        ):
            router.handle_mirror_question_to_slack(self._payload())

        router.handle_resolve_answered_question(
            {"tool_name": "AskUserQuestion", "session_id": "sess-interactive", "tool_use_id": "tu-1"}
        )

        row = DeferredQuestion.objects.get()
        assert row.status == DeferredQuestion.STATUS_ANSWERED
        assert DeferredQuestion.live_for_reply(channel="D0OWNER", after_ts="1779990002.000001") is None

    def test_the_resolver_is_registered_on_posttooluse(self) -> None:
        assert router.handle_resolve_answered_question in router._HANDLERS["PostToolUse"]


class TestPresentLoopDrivenTurnDeniesAndCaptures(_CapturedStdoutTestCase):
    """Present mode + loop-driven + not-live-turn → deny + capture (#1174).

    The core bug: a loop-driven AskUserQuestion in present mode rendered
    in-client and blocked the suspended session — a Slack reply could
    never reach it. The fix denies the tool call (so the agent narrates
    and proceeds), captures a generation-stamped mirror-linked
    ``DeferredQuestion``, and stores the posted Slack ts so the matcher
    can bind a later reply.
    """

    def _pin_state_dir(self) -> None:
        state_dir = patch.object(router, "STATE_DIR", self.tmp_path)
        state_dir.start()
        self.addCleanup(state_dir.stop)

    def _payload(self, **extra: str) -> dict:
        payload: dict = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}, {"label": "No"}]}]},
        }
        payload.update(extra)
        return payload

    def test_loop_driven_present_turn_denies_with_row_id(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_resolved_away_mode", return_value=False),
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_perform_slack_post", return_value="1700.0001"),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
            patch.object(router, "_read_dm_channel_cache", return_value="D-cached"),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-loop"))
        assert verdict is True
        out = json.loads(self.drain_stdout().strip())
        assert out["permissionDecision"] == "deny"
        row = DeferredQuestion.objects.latest("created_at")
        assert f"#{row.pk}" in out["permissionDecisionReason"]
        assert "additionalContext" in out["permissionDecisionReason"]
        assert row.slack_ts == "1700.0001"
        assert row.slack_channel == "D-cached"
        assert row.generation == 1

    def test_live_user_turn_mirrors_without_deny(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_resolved_away_mode", return_value=False),
            patch.object(router, "_is_live_user_turn", return_value=True),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_perform_slack_post", return_value="1700.0002") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-live"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        mock_post.assert_called_once()
        # #3642: the interactive arm records the question too, so Slack can answer it.
        assert DeferredQuestion.objects.count() == 1

    def test_attended_non_owner_turn_mirrors_without_deny(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_resolved_away_mode", return_value=False),
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=False),
            patch.object(router, "_perform_slack_post", return_value="1700.0003") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-attended"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        mock_post.assert_called_once()
        # #3642: the interactive arm records the question too, so Slack can answer it.
        assert DeferredQuestion.objects.count() == 1

    def test_supersession_marks_prior_generation_stale(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_resolved_away_mode", return_value=False),
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_perform_slack_post", side_effect=["1700.0001", "1700.0005"]),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
            patch.object(router, "_read_dm_channel_cache", return_value="D-cached"),
        ):
            router.handle_mirror_question_to_slack(self._payload(session_id="s-loop", run_id="r1"))
            self.drain_stdout()
            router.handle_mirror_question_to_slack(self._payload(session_id="s-loop", run_id="r1"))
        self.drain_stdout()
        rows = list(DeferredQuestion.objects.order_by("generation"))
        assert len(rows) == 2
        assert rows[0].resolved_via == "stale"
        assert rows[0].is_pending is False
        assert rows[1].generation == 2
        assert rows[1].is_pending is True

    def test_teatree_unavailable_fails_open_no_deny(self) -> None:
        with (
            patch.object(router, "_resolved_away_mode", return_value=False),
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_capture_and_defer_question", return_value=None),
            patch.object(router, "_perform_slack_post", return_value="1700.0001"),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-loop"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""


class TestRouterDomainWiring(TestCase):
    """The router owns the platform→domain edges the leaf must not carry.

    The ``teatree.hooks.slack_mirror`` leaf is a pure platform leaf, so the
    Slack ``post`` (``teatree.backends.slack``) and the active-DM-thread lookup
    (``teatree.core``) are built HERE and injected. These pin that wiring.
    """

    def test_http_poster_is_slack_client_post_with_no_retry(self) -> None:
        poster = router._slack_http_poster()
        client = poster.__self__
        assert client._max_retries == 0
        assert client._timeout == pytest.approx(wiring._SLACK_POST_TIMEOUT_SECONDS)

    def test_active_dm_thread_resolves_most_recent_ref_for_channel(self) -> None:
        from teatree.core.models import IncomingEvent  # noqa: PLC0415

        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="D-cached",
            thread_ref="1700000000.0009",
            idempotency_key="slack:Ev-mirror",
        )

        assert router._active_dm_thread_for_channel("D-cached") == "1700000000.0009"

    def test_active_dm_thread_empty_when_no_channel(self) -> None:
        assert router._active_dm_thread_for_channel("") == ""

    def test_active_dm_thread_empty_when_django_unavailable(self) -> None:
        with patch.object(router, "bootstrap_teatree_django", return_value=False):
            assert router._active_dm_thread_for_channel("D-cached") == ""


class TestHooksJsonWiring(TestCase):
    def test_askuserquestion_matcher_lives_on_pretooluse(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        pre_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PreToolUse", [])]
        post_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PostToolUse", [])]
        assert "AskUserQuestion" in pre_matchers
        assert "AskUserQuestion" not in post_matchers

    def test_askuserquestion_hook_timeout_allows_sync_post(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        ask_entry = next(
            entry for entry in hooks_config["hooks"]["PreToolUse"] if entry.get("matcher") == "AskUserQuestion"
        )
        # Synchronous post needs to fit pass-show + (cache hit) chat.postMessage
        # under the timeout. 3s was too tight; 5s+ keeps a safety margin.
        assert ask_entry["hooks"][0]["timeout"] >= 5
