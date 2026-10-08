"""A routed task whose candidates are only waiting parks until the first can run; a permanent refusal fails."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.agents.harness_registry import HarnessCapabilities, HarnessSpec, register_harness
from teatree.agents.runner import _resolve_backend_or_failure
from teatree.agents.skill_routing import QUOTA_AUTH_HOLD, record_route_unavailable
from teatree.config.agent_spawn import AgentRouteCandidate
from teatree.core.models import LIMIT_PARKED_PREFIX, Mode, ModeOverride, Session, Task, TaskAttempt, UsageWindowState
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    MANAGED,
    StubHarness,
    register_stub_harnesses,
    route_config,
    routed_by,
)
from tests.teatree_agents._sdk_fake import FakeHarnessSession

_PHASE = "debugging"


class TestRouteCandidatesThatAreOnlyWaiting(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, MANAGED, CLAUDE_LIKE)
        Mode.objects.create(name="token-outage", entries={"inbox": True})

    def _task(self, *, claude_only: bool = False) -> Task:
        ticket = planned_ticket(extra={"claude_only": True} if claude_only else {})
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=_PHASE)

    @staticmethod
    def _hold_codex(task: Task) -> None:
        candidate = AgentRouteCandidate(MANAGED, f"{MANAGED}-model", "managed_chatgpt")
        overlay = task.ticket.overlay or ""
        record_route_unavailable(
            overlay, candidate, "ChatGPT quota exhausted", phase=_PHASE, retry_after=QUOTA_AUTH_HOLD
        )

    @staticmethod
    def _window_claude(hours: int) -> UsageWindowState:
        return UsageWindowState.record_limit(
            lane="", cause="subscription_session", resets_at=timezone.now() + timedelta(hours=hours)
        )

    def _dispatch(self, task: Task, *harnesses: str) -> object:
        with routed_by(route_config("route-skill", *harnesses)):
            return _resolve_backend_or_failure(task, phase=_PHASE, skills=["route-skill"])

    def test_a_held_codex_and_a_windowed_claude_park_until_the_hold_lifts(self) -> None:
        task = self._task()
        before = timezone.now()
        self._hold_codex(task)
        self._window_claude(3)

        attempt = self._dispatch(task, MANAGED, CLAUDE_LIKE)

        task.refresh_from_db()
        assert isinstance(attempt, TaskAttempt)
        assert task.status == Task.Status.PENDING
        assert before + QUOTA_AUTH_HOLD <= task.not_before <= timezone.now() + QUOTA_AUTH_HOLD
        assert attempt.error.startswith(LIMIT_PARKED_PREFIX)
        assert f"{MANAGED}: ChatGPT quota exhausted" in attempt.error
        assert f"{CLAUDE_LIKE}: subscription_session window" in attempt.error
        assert list(task.attempts.values_list("pk", flat=True)) == [attempt.pk]
        assert ModeOverride.objects.current() is None

    def test_a_barred_codex_with_no_fallback_still_fails(self) -> None:
        task = self._task(claude_only=True)

        attempt = self._dispatch(task, MANAGED)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.error.startswith("No available harness")

    def test_a_barred_codex_and_a_windowed_claude_park_until_the_window_resets(self) -> None:
        task = self._task(claude_only=True)
        window = self._window_claude(3)

        attempt = self._dispatch(task, MANAGED, CLAUDE_LIKE)

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == window.resets_at
        assert f"{MANAGED}: ticket is claude_only" in attempt.error

    def test_the_third_consecutive_park_on_a_route_hold_fails_instead(self) -> None:
        task = self._task()
        self._hold_codex(task)
        self._window_claude(3)

        first = self._dispatch(task, MANAGED, CLAUDE_LIKE)
        second = self._dispatch(task, MANAGED, CLAUDE_LIKE)
        third = self._dispatch(task, MANAGED, CLAUDE_LIKE)

        task.refresh_from_db()
        assert first.error.startswith(LIMIT_PARKED_PREFIX)
        assert second.error.startswith(LIMIT_PARKED_PREFIX)
        assert third.error.startswith("No available harness")
        assert task.status == Task.Status.FAILED

    def test_a_short_probe_answer_is_no_hold_so_a_lone_candidate_still_fails(self) -> None:
        capabilities = HarnessCapabilities(managed_lane=True)
        register_harness(
            HarnessSpec(
                name=MANAGED,
                factory=lambda _context: StubHarness(capabilities, FakeHarnessSession),
                capabilities=capabilities,
                allows_provider=False,
                unavailable_reason=lambda _context: "no Codex login in the private home",
            )
        )
        task = self._task()

        attempt = self._dispatch(task, MANAGED)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.error.startswith("No available harness")
