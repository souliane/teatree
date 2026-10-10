"""AskUserQuestion routing on PreToolUse — deny the loop-driven arm, pass the attended one.

A question asked in Claude Code is answered in Claude Code and nowhere else (#4673):
the attended arms send nothing and record nothing. A LOOP-DRIVEN call — which has no
terminal to render into — is captured as an INTERNAL row and never DM'd (#5096); the
agent re-records it with ``--decision`` only for a decision the owner alone makes.
"""

import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import hooks.scripts.hook_router as router
from teatree.core.modelkit.owner_decision import OWNER_QUESTION_ROUTE, OwnerDecision
from teatree.core.models.deferred_question import DeferredQuestion
from tests._owner_channel import owner_card


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
        assert router.handle_mirror_question_to_slack(self._question_payload()) is False

    def test_ignores_other_tools(self) -> None:
        router.handle_mirror_question_to_slack({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert DeferredQuestion.objects.count() == 0

    def test_records_no_row_so_the_tick_drain_cannot_repost_it(self) -> None:
        router.handle_mirror_question_to_slack(self._question_payload())
        assert DeferredQuestion.objects.count() == 0
        assert not DeferredQuestion.unmirrored_pending().exists()

    def test_nothing_is_bindable_from_slack(self) -> None:
        router.handle_mirror_question_to_slack(self._question_payload())
        assert DeferredQuestion.live_for_reply(channel="D0OWNER", after_ts="1779990002.000001") is None

    def test_an_empty_question_list_is_a_no_op(self) -> None:
        verdict = router.handle_mirror_question_to_slack(
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
        )
        assert verdict is False
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

    def test_loop_driven_present_turn_denies_and_records_an_internal_row(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-loop", tool_use_id="tu-9"))
        assert verdict is True
        out = json.loads(self.drain_stdout().strip())
        assert out["permissionDecision"] == "deny"
        row = DeferredQuestion.objects.latest("created_at")
        assert f"#{row.pk}" in out["permissionDecisionReason"]
        assert OWNER_QUESTION_ROUTE in out["permissionDecisionReason"]
        assert "credentials" in out["permissionDecisionReason"]
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.generation == 1
        assert row not in DeferredQuestion.unmirrored_pending()

    def test_live_user_turn_renders_without_deny_or_delivery(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=True),
            patch.object(router, "_session_drives_loop", return_value=True),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-live"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        assert DeferredQuestion.objects.count() == 0

    def test_attended_non_owner_turn_renders_without_deny_or_delivery(self) -> None:
        self._pin_state_dir()
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=False),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-attended"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""
        assert DeferredQuestion.objects.count() == 0

    def _ask(self, question: str, *, delivered_ts: str, **extra: str) -> None:
        """Drive one loop-driven ask, then stamp the mirror the drain lands at *delivered_ts* (``""`` = undelivered)."""
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
        ):
            router.handle_mirror_question_to_slack(self._payload(question, **extra))
        self.drain_stdout()
        # The hook records UN-MIRRORED (#4673); delivery — and the ts #4721 keys on — is the drain's.
        if delivered_ts:
            DeferredQuestion.objects.latest("pk").mark_mirrored(channel="D-cached", slack_ts=delivered_ts)

    def _owner_row(self, question: str, *, delivered_ts: str = "", **scope: str) -> DeferredQuestion:
        """A pending owner row from before #5096, when a loop-driven capture still reached the owner."""
        row = DeferredQuestion.record(
            question, card=owner_card(OwnerDecision.PRODUCT_SCOPE, "the ticket is silent"), **scope
        )
        if delivered_ts:
            row.mark_mirrored(channel="D-cached", slack_ts=delivered_ts)
        return row

    def test_supersession_marks_a_prior_owner_row_stale(self) -> None:
        """The control: an UNDELIVERED same-run owner row is still swept, so #4721 did not disable the feature."""
        self._pin_state_dir()
        prior = self._owner_row("Ship it?", session_id="s-loop", run_id="r1")
        self._ask("Merge it?", delivered_ts="", session_id="s-loop", run_id="r1")

        prior.refresh_from_db()
        assert prior.resolved_via == "stale"
        assert prior.is_pending is False
        assert DeferredQuestion.objects.latest("pk").is_pending is True

    def test_a_delivered_row_is_never_superseded(self) -> None:
        """Its Slack thread may already carry the owner's reply (#4721)."""
        self._pin_state_dir()
        prior = self._owner_row("Ship it?", delivered_ts="1700.0001", session_id="s-loop", run_id="r1")
        self._ask("Merge it?", delivered_ts="", session_id="s-loop", run_id="r1")

        prior.refresh_from_db()
        assert prior.is_pending is True, "a mirrored row awaiting a Slack reply was mass-dismissed"

    def test_a_run_id_less_payload_supersedes_nothing(self) -> None:
        """``_run_id`` degrading to the session id widened the sweep to the whole session (#4721)."""
        self._pin_state_dir()
        prior = self._owner_row("Ship it?", session_id="s-loop")
        self._ask("Merge it?", delivered_ts="", session_id="s-loop")

        prior.refresh_from_db()
        assert prior.is_pending is True, "a supersession that cannot name its run swept the session"

    def test_an_internal_row_in_the_same_run_is_never_superseded(self) -> None:
        """The box's own health queue is not the owner's, even sharing a (session, run)."""
        self._pin_state_dir()
        internal = DeferredQuestion.record("repair-loop stalled", session_id="s-loop", run_id="r1")
        self._ask("Ship it?", delivered_ts="", session_id="s-loop", run_id="r1")

        internal.refresh_from_db()
        assert internal.is_pending is True

    def test_another_sessions_row_is_never_superseded(self) -> None:
        """Pins the scope as per-session — the guard no test held before #4721."""
        self._pin_state_dir()
        foreign = self._owner_row("Is this another session?", session_id="s-other", run_id="r1")
        self._ask("Ship it?", delivered_ts="", session_id="s-loop", run_id="r1")

        foreign.refresh_from_db()
        assert foreign.is_pending is True

    def test_teatree_unavailable_fails_open_no_deny(self) -> None:
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=True),
            patch.object(router, "_capture_and_defer_question", return_value=None),
        ):
            verdict = router.handle_mirror_question_to_slack(self._payload(session_id="s-loop"))
        assert verdict is False
        assert self.drain_stdout().strip() == ""


class TestAttendedArmSupersessionKeepsTheSameGuards(_CapturedStdoutTestCase):
    """The attended arm sweeps through the SAME three narrowings as capture (#4721, #4673).

    ``_supersede_pending_questions`` reaches the same rows ``_capture_and_defer_question``
    does, so an unnarrowed sweep here re-opens every way #4721 closed for the owner to lose
    a question. The last three cases below are RED without :meth:`DeferredQuestion.supersedable`;
    the first two pin the scope and the feature so the narrowing cannot become a disablement.
    """

    def _attended_ask(self, question: str = "Ship it?", **extra: str) -> None:
        """Drive one attended non-owner ask — the in-client arm that sweeps and records nothing."""
        payload: dict = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": question, "options": [{"label": "Yes"}]}]},
        }
        payload.update(extra)
        with (
            patch.object(router, "_is_live_user_turn", return_value=False),
            patch.object(router, "_session_drives_loop", return_value=False),
        ):
            assert router.handle_mirror_question_to_slack(payload) is False
        self.drain_stdout()

    def test_an_undelivered_same_run_row_is_still_superseded(self) -> None:
        """The control: the narrowing must not amount to switching the sweep off."""
        stale = DeferredQuestion.record(
            "Ship it?",
            session_id="s-att",
            run_id="r1",
            card=owner_card(OwnerDecision.PRODUCT_SCOPE, "the ticket is silent"),
        )
        self._attended_ask(session_id="s-att", run_id="r1")

        stale.refresh_from_db()
        assert stale.is_pending is False, "the owner is still nagged for a decision made in-client"
        assert stale.resolved_via == "stale"

    def test_another_sessions_row_is_never_superseded(self) -> None:
        foreign = DeferredQuestion.record(
            "Is this another session?",
            session_id="s-other",
            run_id="r1",
            card=owner_card(OwnerDecision.PRODUCT_SCOPE, "the ticket is silent"),
        )
        self._attended_ask(session_id="s-att", run_id="r1")

        foreign.refresh_from_db()
        assert foreign.is_pending is True

    def test_a_delivered_row_is_never_superseded(self) -> None:
        """Dismissing it strands the owner's in-flight Slack reply on a row nothing can bind it to."""
        delivered = DeferredQuestion.record(
            "Ship it?",
            session_id="s-att",
            run_id="r1",
            slack_ts="1700.0001",
            slack_channel="D-cached",
            card=owner_card(OwnerDecision.PRODUCT_SCOPE, "the ticket is silent"),
        )
        self._attended_ask(session_id="s-att", run_id="r1")

        delivered.refresh_from_db()
        assert delivered.is_pending is True, "a mirrored row awaiting a Slack reply was dismissed"

    def test_an_internal_row_in_the_same_run_is_never_superseded(self) -> None:
        """The box's own health queue is not the owner's, even sharing a (session, run)."""
        internal = DeferredQuestion.record("repair-loop stalled", session_id="s-att", run_id="r1")
        self._attended_ask(session_id="s-att", run_id="r1")

        internal.refresh_from_db()
        assert internal.is_pending is True

    def test_a_run_id_less_ask_supersedes_nothing(self) -> None:
        """A real PreToolUse payload carries no run id, so an unscoped sweep is the COMMON case."""
        backlog = DeferredQuestion.record("Ship it?", session_id="s-att")
        self._attended_ask(session_id="s-att")

        backlog.refresh_from_db()
        assert backlog.is_pending is True, "an unscoped ask erased the session's whole pending backlog"


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
