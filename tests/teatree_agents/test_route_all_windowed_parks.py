"""A routed task whose candidates are only waiting parks until the first can run; a permanent refusal fails."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness_dispatch as harness_dispatch_mod
from teatree.agents import harness_registry
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessSpec,
    NoAvailableHarnessError,
    register_harness,
)
from teatree.agents.runner import _resolve_backend_or_failure
from teatree.agents.skill_routing import QUOTA_AUTH_HOLD, record_route_unavailable
from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.config.agent_spawn import AgentRouteCandidate
from teatree.core.models import LIMIT_PARKED_PREFIX, Mode, ModeOverride, Session, Task, TaskAttempt, UsageWindowState
from teatree.llm.credentials import CredentialError
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

    def _dispatch(self, task: Task, *harnesses: str) -> TaskAttempt:
        with routed_by(route_config("route-skill", *harnesses)):
            outcome = _resolve_backend_or_failure(task, phase=_PHASE, skills=["route-skill"])
        assert isinstance(outcome, TaskAttempt)
        return outcome

    def _register_resolved_paid_harness(self) -> None:
        name = "resolved_paid"
        capabilities = HarnessCapabilities()
        register_harness(
            HarnessSpec(
                name=name,
                factory=lambda _context: StubHarness(capabilities, FakeHarnessSession),
                capabilities=capabilities,
                valid_providers=frozenset({AgentHarnessProvider.API_KEY.value}),
            )
        )
        self.addCleanup(harness_registry._REGISTRY.pop, name, None)

    def _register_metered_harness(self, name: str) -> None:
        capabilities = HarnessCapabilities(metered_lane=True)
        register_harness(
            HarnessSpec(
                name=name,
                factory=lambda _context: StubHarness(capabilities, FakeHarnessSession),
                capabilities=capabilities,
                allows_provider=False,
            )
        )
        self.addCleanup(harness_registry._REGISTRY.pop, name, None)

    def _another_lane_can_dispatch(self, *harnesses: str) -> bool:
        settings = replace(
            get_effective_settings(None),
            agent_harness=MANAGED,
            agent_harness_provider=None,
            agent_route_paid_fallback=False,
        )
        with (
            patch.object(harness_dispatch_mod, "get_effective_settings", return_value=settings),
            routed_by(route_config("route-skill", *harnesses)),
        ):
            return harness_dispatch_mod.another_lane_can_dispatch(TaskAttempt.Lane.MANAGED)

    def test_gated_metered_fallback_does_not_count_as_an_open_lane(self) -> None:
        self._register_metered_harness("metered_side")

        assert not self._another_lane_can_dispatch(MANAGED, "metered_side")

    def test_a_metered_primary_route_counts_as_an_open_lane_without_authorization(self) -> None:
        self._register_metered_harness("metered_side")

        assert self._another_lane_can_dispatch("metered_side", MANAGED)

    def test_overlay_phase_candidates_gate_a_later_metered_lane(self) -> None:
        self._register_metered_harness("metered_phase")
        settings = replace(get_effective_settings(None), agent_route_paid_fallback=False)
        context = HarnessBuildContext(settings=settings, phase=_PHASE)

        with pytest.raises(NoAvailableHarnessError) as refused:
            harness_dispatch_mod.candidate_selection(context, ["missing_first", "metered_phase"])

        assert "agent_route_paid_fallback" in str(refused.value)

    def test_overlay_phase_candidates_refuse_a_later_metered_lane_without_settings(self) -> None:
        self._register_metered_harness("metered_phase")

        with pytest.raises(NoAvailableHarnessError) as refused:
            harness_dispatch_mod.candidate_selection(
                HarnessBuildContext(phase=_PHASE), ["missing_first", "metered_phase"]
            )

        assert "agent_route_paid_fallback" in str(refused.value)

    def test_a_later_candidate_without_credentials_is_rejected_not_raised(self) -> None:
        self._register_resolved_paid_harness()
        context = HarnessBuildContext(settings=get_effective_settings(None), phase=_PHASE)

        with (
            patch.object(
                harness_dispatch_mod, "resolve_credential_provider", side_effect=CredentialError("no API key")
            ),
            pytest.raises(NoAvailableHarnessError) as refused,
        ):
            harness_dispatch_mod.candidate_selection(context, ["missing_first", "resolved_paid"])

        assert "no API key" in str(refused.value)

    def test_a_route_candidate_without_credentials_is_not_an_open_lane(self) -> None:
        self._register_resolved_paid_harness()

        with patch.object(
            harness_dispatch_mod, "resolve_credential_provider", side_effect=CredentialError("no API key")
        ):
            assert not self._another_lane_can_dispatch(MANAGED, "resolved_paid")

    def test_overlay_phase_candidates_allow_a_metered_first_candidate_without_authorization(self) -> None:
        self._register_metered_harness("metered_phase")
        context = HarnessBuildContext(settings=get_effective_settings(None), phase=_PHASE)

        selection = harness_dispatch_mod.candidate_selection(context, ["metered_phase", MANAGED])

        assert selection is not None
        assert selection.spec.name == "metered_phase"

    def test_overlay_phase_candidates_allow_a_later_free_lane_without_settings(self) -> None:
        selection = harness_dispatch_mod.candidate_selection(
            HarnessBuildContext(phase=_PHASE), ["missing_first", MANAGED]
        )

        assert selection is not None
        assert selection.spec.name == MANAGED

    def test_a_long_codex_window_parks_through_the_metered_gate_until_it_resets(self) -> None:
        task = self._task()
        self._register_resolved_paid_harness()
        window = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.MANAGED, cause="subscription_session", resets_at=timezone.now() + timedelta(hours=3)
        )
        with patch.object(
            harness_dispatch_mod, "resolve_credential_provider", return_value=AgentHarnessProvider.API_KEY
        ):
            attempts = [self._dispatch(task, MANAGED, "resolved_paid") for _ in range(3)]

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == window.resets_at
        assert all(attempt.error.startswith(LIMIT_PARKED_PREFIX) for attempt in attempts)
        assert "resolved_paid: metered route fallback requires agent_route_paid_fallback" in attempts[0].error
        assert not task.attempts.filter(lane=TaskAttempt.Lane.METERED).exists()

    def test_a_gated_metered_candidate_after_a_barred_one_fails_after_two_parks_with_a_readable_reason(self) -> None:
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
        self._register_resolved_paid_harness()
        with patch.object(
            harness_dispatch_mod, "resolve_credential_provider", return_value=AgentHarnessProvider.API_KEY
        ):
            attempts = [self._dispatch(task, MANAGED, "resolved_paid") for _ in range(3)]

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert [attempt.error.startswith(LIMIT_PARKED_PREFIX) for attempt in attempts] == [True, True, False]
        assert "agent_route_paid_fallback" in attempts[-1].error

    def test_a_held_codex_and_a_windowed_claude_park_until_the_hold_lifts(self) -> None:
        task = self._task()
        before = timezone.now()
        self._hold_codex(task)
        self._window_claude(3)

        attempt = self._dispatch(task, MANAGED, CLAUDE_LIKE)

        task.refresh_from_db()
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

    def test_the_third_consecutive_usage_limit_hold_remains_parked(self) -> None:
        task = self._task()
        self._hold_codex(task)
        self._window_claude(3)

        first = self._dispatch(task, MANAGED, CLAUDE_LIKE)
        second = self._dispatch(task, MANAGED, CLAUDE_LIKE)
        third = self._dispatch(task, MANAGED, CLAUDE_LIKE)

        task.refresh_from_db()
        assert first.error.startswith(LIMIT_PARKED_PREFIX)
        assert second.error.startswith(LIMIT_PARKED_PREFIX)
        assert third.error.startswith(LIMIT_PARKED_PREFIX)
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before > timezone.now()

    def test_a_real_run_between_hold_parks_starts_a_new_count(self) -> None:
        task = self._task()
        self._hold_codex(task)
        self._window_claude(3)
        self._dispatch(task, MANAGED, CLAUDE_LIKE)
        self._dispatch(task, MANAGED, CLAUDE_LIKE)
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error="harness_crash")

        attempt = self._dispatch(task, MANAGED, CLAUDE_LIKE)

        task.refresh_from_db()
        assert attempt.error.startswith(LIMIT_PARKED_PREFIX)
        assert task.status == Task.Status.PENDING

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
