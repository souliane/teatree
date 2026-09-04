"""AskUserQuestion routing on PreToolUse — deny the loop-driven arm, pass the attended one.

A question asked in Claude Code is answered in Claude Code and nowhere else (#4673):
the attended arms send nothing and record nothing, because an un-mirrored row is what
the tick drain would pick up, re-posting the question to Slack one cadence later. Only
a LOOP-DRIVEN call — which has no terminal to render into — is captured and delivered,
through the single ``drain_unmirrored_deferred_questions`` -> ``notify_user`` egress.
"""

import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import hooks.scripts.hook_router as router
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


class TestAttendedTurnSendsNothingAndRecordsNothing(TestCase):
    """The attended arms are inert: in-client render, no Slack, no durable row (#4673).

    Recording an un-mirrored row here would be the duplication with a delay — the tick
    drain selects exactly ``slack_ts == ""`` and would post it to the owner's DM.
    """

    def setUp(self) -> None:
        live_turn = patch.object(router, "_is_live_user_turn", lambda _data: True)
        live_turn.start()
        self.addCleanup(live_turn.stop)

    def _question_payload(self) -> dict:
        return {
            "tool_name": "AskUserQuestion",
            "session_id": "sess-interactive",
            "tool_use_id": "tu-1",
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
        with patch.object(router, "_kick_question_drain") as kick:
            result = router.handle_mirror_question_to_slack(self._question_payload())
        assert result is False
        kick.assert_not_called()

    def test_ignores_other_tools(self) -> None:
        with patch.object(router, "_kick_question_drain") as kick:
            router.handle_mirror_question_to_slack({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        kick.assert_not_called()
        assert DeferredQuestion.objects.count() == 0

    def test_records_no_row_so_the_tick_drain_cannot_repost_it(self) -> None:
        with patch.object(router, "_kick_question_drain"):
            router.handle_mirror_question_to_slack(self._question_payload())
        assert DeferredQuestion.objects.count() == 0
        assert not DeferredQuestion.unmirrored_pending().exists()

    def test_nothing_is_bindable_from_slack(self) -> None:
        with patch.object(router, "_kick_question_drain"):
            router.handle_mirror_question_to_slack(self._question_payload())
        assert DeferredQuestion.live_for_reply(channel="D0OWNER", after_ts="1779990002.000001") is None

    def test_an_empty_question_list_is_a_no_op(self) -> None:
        with patch.object(router, "_kick_question_drain") as kick:
            verdict = router.handle_mirror_question_to_slack(
                {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
            )
        assert verdict is False
        kick.assert_not_called()
        assert DeferredQuestion.objects.count() == 0

    def test_the_in_client_answer_resolver_is_gone_from_posttooluse(self) -> None:
        # It existed only to stop a Slack reply and an in-client answer both applying;
        # with no attended row there is nothing for it to resolve.
        assert not hasattr(router, "handle_resolve_answered_question")


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

    def _payload(self, question: str = "Ship it?", **extra: str) -> dict:
        payload: dict = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": question, "options": [{"label": "Yes"}, {"label": "No"}]}]},
        }
        payload.update(extra)
        return payload

    def test_loop_driven_present_turn_denies_with_row_id(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_kick_question_drain") as kick,
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-loop", tool_use_id="tu-9"))
        assert verdict is True
        out = json.loads(self.drain_stdout().strip())
        assert out["permissionDecision"] == "deny"
        row = DeferredQuestion.objects.latest("created_at")
        assert f"#{row.pk}" in out["permissionDecisionReason"]
        assert "additionalContext" in out["permissionDecisionReason"]
        assert row.slack_ts == "", "the hook must not post; the drain stamps the coordinates"
        assert row.generation == 1
        assert row in DeferredQuestion.unmirrored_pending()
        kick.assert_called_once_with(row.stable_notify_ref)

    def test_live_user_turn_renders_without_deny_or_delivery(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=True),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_kick_question_drain") as kick,
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-live"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        kick.assert_not_called()
        assert DeferredQuestion.objects.count() == 0

    def test_attended_non_owner_turn_renders_without_deny_or_delivery(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=False),
            patch.object(router, "_kick_question_drain") as kick,
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-attended"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        kick.assert_not_called()
        assert DeferredQuestion.objects.count() == 0

    def test_supersession_marks_prior_generation_stale(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_kick_question_drain"),
        ):
            router.handle_mirror_question_to_slack(self._payload(session_id="s-loop", run_id="r1"))
            self.drain_stdout()
            # A byte-identical re-ask is a harness RETRY (#4202) and binds to the live row,
            # so supersession is only reachable from a genuinely different question.
            router.handle_mirror_question_to_slack(self._payload("Merge it?", session_id="s-loop", run_id="r1"))
        self.drain_stdout()
        rows = list(DeferredQuestion.objects.order_by("generation"))
        assert len(rows) == 2
        assert rows[0].resolved_via == "stale"
        assert rows[0].is_pending is False
        assert rows[1].generation == 2
        assert rows[1].is_pending is True

    def test_teatree_unavailable_fails_open_no_deny(self) -> None:
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_capture_and_defer_question", return_value=(None, "")),
            patch.object(router, "_kick_question_drain") as kick,
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-loop"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        kick.assert_not_called()


class TestHooksJsonWiring(TestCase):
    def test_askuserquestion_matcher_lives_on_pretooluse(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        pre_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PreToolUse", [])]
        post_matchers = [entry.get("matcher", "") for entry in hooks_config["hooks"].get("PostToolUse", [])]
        assert "AskUserQuestion" in pre_matchers
        assert "AskUserQuestion" not in post_matchers

    def test_askuserquestion_hook_timeout_allows_the_durable_capture(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        hooks_config = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        ask_entry = next(
            entry for entry in hooks_config["hooks"]["PreToolUse"] if entry.get("matcher") == "AskUserQuestion"
        )
        # The hook bootstraps Django and writes the row; delivery is detached, so the
        # budget covers the ORM write rather than a Slack round trip.
        assert ask_entry["hooks"][0]["timeout"] >= 5
