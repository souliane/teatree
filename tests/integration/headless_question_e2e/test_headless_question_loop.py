"""Headless ask-loop end to end: needs_user_input → Slack → reply → headless resume.

The SDK/headless lane closing the outbound question loop with only the
network mocked. There is no human at the harness, so:

- a headless task returns ``needs_user_input`` and STOPS → a correlated,
mirror-pending ``DeferredQuestion`` is recorded (no interactive followup);
- the tick-level ``DeferredQuestionPosterScanner`` posts it to the user's DM
and stamps the mirror coordinates (idempotent on a re-tick);
- the user's Slack reply binds to the live question and re-queues a HEADLESS
resume carrying the answer + the captured resume session — continue from the
decision point, not a fresh-from-scratch run, no interactive task.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

import teatree.agents.headless as headless_mod
from teatree.agents._headless_options import _get_resume_session_id
from teatree.agents.headless import run_headless
from teatree.core import notify as notify_module
from teatree.core.models import BotPing, ConfigSetting, DeferredQuestion, PendingChatInjection, Session, Task, Ticket
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.scanners.deferred_question_poster import DeferredQuestionPosterScanner
from tests.teatree_agents._sdk_fake import fake_sdk as _fake_sdk
from tests.teatree_agents._sdk_fake import success_stream as _success_stream

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_RESUME_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_CHANNEL = "D-USER"
_QUESTION_TS = "1700000000.000001"
_REPLY_TS = "1700000000.000050"


@dataclass
class FakeBackend:
    """A self-DM Slack double for both the bot→user post and the ✅ react."""

    posted: list[str] = field(default_factory=list)
    reacts: list[tuple[str, str, str]] = field(default_factory=list)
    route_token: str = "self"

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return True

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return _CHANNEL

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        _ = (channel, thread_ts)
        self.posted.append(text)
        return {"ok": True, "ts": _QUESTION_TS}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
        self.reacts.append((channel, ts, emoji))
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = channel
        return f"https://slack/{ts}"


class TestHeadlessQuestionLoop:
    def _run_parked_headless_task(self) -> Task:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id=_RESUME_UUID)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        result = {
            "summary": "Blocked on a design decision",
            "needs_user_input": True,
            "user_input_reason": "Which DB host should the new connection pool target?",
        }
        with (
            _fake_sdk(_success_stream(result, session_id=_RESUME_UUID)),
            patch.object(headless_mod, "_provider_child_env", return_value=None),
        ):
            run_headless(task, phase="coding", overlay_skill_metadata={})
        task.refresh_from_db()
        return task

    def test_full_loop_park_post_reply_resume(self) -> None:
        # 1. Headless STOP → correlated, un-mirrored DeferredQuestion; no interactive task.
        parked = self._run_parked_headless_task()
        assert parked.status == Task.Status.COMPLETED
        assert not Task.objects.filter(execution_target=Task.ExecutionTarget.INTERACTIVE).exists()
        question = DeferredQuestion.objects.get()
        assert question.parked_task_id == parked.pk
        assert "Which DB host" in question.question
        assert question.slack_ts == ""

        # 2. Poster scanner posts it and stamps the mirror coordinates (idempotent on re-tick).
        backend = FakeBackend()
        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch.object(notify_module, "resolve_user_id", return_value="U_ME"),
        ):
            DeferredQuestionPosterScanner().scan()
            DeferredQuestionPosterScanner().scan()
        assert len(backend.posted) == 1
        question.refresh_from_db()
        assert question.slack_channel == _CHANNEL
        assert question.slack_ts == _QUESTION_TS
        assert BotPing.objects.filter(
            idempotency_key=f"mirror-deferred-question:{question.stable_notify_ref}",
            status=BotPing.Status.SENT,
        ).exists()

        # 3. Inbound reply binds → HEADLESS resume carrying the answer + the captured session.
        PendingChatInjection.record(channel=_CHANNEL, slack_ts=_REPLY_TS, text="use postgres-1", user_id="U_ME")
        AskUserQuestionReplyScanner(backend=backend, overlay="").scan()

        question.refresh_from_db()
        assert question.answer_text == "use postgres-1"
        assert question.resolved_via == DeferredQuestion.ResolvedVia.SLACK
        assert (_CHANNEL, _REPLY_TS, "white_check_mark") in backend.reacts

        resume = parked.child_tasks.get()
        assert resume.execution_target == Task.ExecutionTarget.HEADLESS
        assert resume.parent_task_id == parked.pk
        assert "use postgres-1" in resume.execution_reason
        assert _get_resume_session_id(resume) == _RESUME_UUID
        assert not Task.objects.filter(execution_target=Task.ExecutionTarget.INTERACTIVE).exists()
