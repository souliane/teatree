"""``teatree.agents.credential_policy`` — ride the plans, fail over to the meter, come back.

``agent_harness_provider=subscription_then_api_key`` is a POLICY, not a credential: it is
resolved ONCE into one of the two concrete ``claude_sdk`` providers, so every downstream
consumer (the lane map, the child env, ``t3 cost``) keeps seeing only values it already
handles. These tests pin the resolution, the exhaustion rule it reads, and the invariant
that the composite never escapes the policy.
"""

from datetime import datetime, timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents._runner_env import _provider_child_env, system_child_env
from teatree.agents.credential_policy import (
    ALL_ACCOUNTS_EXHAUSTED_CAUSE,
    EXHAUSTION_CAUSES,
    resolve_credential_provider,
    subscription_lane_exhausted,
)
from teatree.agents.harness import resolve_harness
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.runner import _resolve_dispatch_lane
from teatree.config import AgentHarnessProvider
from teatree.config.cross_key_consistency import check_harness_provider_pair
from teatree.config.schema import setting_choices
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket, UsageWindowState
from teatree.credential_config import api_key_lane_available, resolve_eval_credential
from teatree.llm.anthropic_limits import LimitCause
from teatree.llm.credentials import AnthropicSubscriptionCredential
from teatree.loops.usage_window_recovery import recover_windows

_API_KEY_VAR = "ANTHROPIC_API_KEY"
_OAUTH_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_COMPOSITE = AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY
_POLICY_LOGGER = "teatree.agents.credential_policy"


def _claimed_task() -> Task:
    ticket = Ticket.objects.create(issue_url="https://example.com/i/1", role=Ticket.Role.AUTHOR)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, phase="coding")
    Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED)
    task.refresh_from_db()
    return task


def _plan_window(cause: str, *, resets_at: datetime | None = None) -> UsageWindowState:
    return UsageWindowState.record_limit(
        lane=TaskAttempt.Lane.SUBSCRIPTION,
        cause=cause,
        resets_at=resets_at if resets_at is not None else timezone.now() + timedelta(days=7),
    )


def _pin(provider: str) -> None:
    ConfigSetting.objects.set_value("agent_harness_provider", provider)


class CredentialPolicyCase(TestCase):
    """Both credentials resolvable from the env, no stored routing, no ambient pins."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_API_KEY_VAR, "sk-ant-test")
        monkeypatch.setenv(_OAUTH_VAR, "oauth-test")
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self._monkeypatch = monkeypatch


class TestExhaustionRule(CredentialPolicyCase):
    def test_only_the_three_plan_exhaustion_causes_count(self) -> None:
        assert (
            frozenset(
                {
                    LimitCause.SUBSCRIPTION_SESSION.value,
                    LimitCause.SUBSCRIPTION_WEEKLY.value,
                    ALL_ACCOUNTS_EXHAUSTED_CAUSE,
                }
            )
            == EXHAUSTION_CAUSES
        )

    def test_an_uncleared_future_window_reads_exhausted(self) -> None:
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        assert subscription_lane_exhausted() is True

    def test_an_elapsed_window_does_not(self) -> None:
        now = timezone.now()
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE, resets_at=now + timedelta(hours=1))
        assert subscription_lane_exhausted(now=now + timedelta(hours=2)) is False

    def test_a_cleared_window_does_not(self) -> None:
        now = timezone.now()
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE).clear(now)
        assert subscription_lane_exhausted(now=now) is False

    def test_a_metered_lane_window_is_not_the_plan_lane(self) -> None:
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.METERED,
            cause=LimitCause.SUBSCRIPTION_WEEKLY.value,
            resets_at=timezone.now() + timedelta(days=7),
        )
        assert subscription_lane_exhausted() is False


class TestResolvedProvider(CredentialPolicyCase):
    def test_plans_only_never_reaches_the_api_even_when_exhausted(self) -> None:
        _pin(AgentHarnessProvider.SUBSCRIPTION_OAUTH.value)
        _plan_window(LimitCause.SUBSCRIPTION_WEEKLY.value)

        assert resolve_dispatch_harness().provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        assert (
            _resolve_dispatch_lane(resolve_harness(), resolve_dispatch_harness().provider)
            == TaskAttempt.Lane.SUBSCRIPTION
        )

        child = _provider_child_env(resolve_dispatch_harness().provider).env
        assert child is not None
        assert _OAUTH_VAR in child
        assert _API_KEY_VAR not in child

    def test_api_only_never_rides_a_plan(self) -> None:
        _pin(AgentHarnessProvider.API_KEY.value)
        for cause in (None, LimitCause.SUBSCRIPTION_WEEKLY.value):
            with self.subTest(cause=cause):
                if cause is not None:
                    _plan_window(cause)
                assert resolve_dispatch_harness().provider is AgentHarnessProvider.API_KEY
                assert _resolve_dispatch_lane(resolve_harness(), resolve_dispatch_harness().provider) == (
                    TaskAttempt.Lane.METERED
                )

        child = _provider_child_env(resolve_dispatch_harness().provider).env
        assert child is not None
        assert _API_KEY_VAR in child
        assert _OAUTH_VAR not in child

    def test_plans_first_rides_the_plan_until_exhaustion_then_the_api(self) -> None:
        _pin(_COMPOSITE.value)
        harness = resolve_harness()

        assert resolve_dispatch_harness().provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        assert _resolve_dispatch_lane(harness, resolve_dispatch_harness().provider) == TaskAttempt.Lane.SUBSCRIPTION
        plan_child = _provider_child_env(resolve_dispatch_harness().provider).env
        assert plan_child is not None
        assert _OAUTH_VAR in plan_child
        assert _API_KEY_VAR not in plan_child

        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)

        assert resolve_dispatch_harness().provider is AgentHarnessProvider.API_KEY
        assert _resolve_dispatch_lane(harness, resolve_dispatch_harness().provider) == TaskAttempt.Lane.METERED
        metered_child = _provider_child_env(resolve_dispatch_harness().provider).env
        assert metered_child is not None
        assert _API_KEY_VAR in metered_child
        assert _OAUTH_VAR not in metered_child

    def test_every_plan_exhaustion_cause_fails_over(self) -> None:
        _pin(_COMPOSITE.value)
        for cause in sorted(EXHAUSTION_CAUSES):
            with self.subTest(cause=cause):
                _plan_window(cause)
                assert resolve_dispatch_harness().provider is AgentHarnessProvider.API_KEY

    def test_a_transient_or_non_plan_cause_never_fails_over(self) -> None:
        _pin(_COMPOSITE.value)
        for cause in (
            LimitCause.RATE_LIMIT.value,
            LimitCause.API_CREDIT.value,
            LimitCause.PROVIDER_BUDGET.value,
            LimitCause.LEAK_BLOCKED.value,
        ):
            with self.subTest(cause=cause):
                _plan_window(cause)
                assert resolve_dispatch_harness().provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH

    def test_recovery_returns_the_lane_to_plans_and_releases_the_parked_task(self) -> None:
        _pin(_COMPOSITE.value)
        now = timezone.now()
        reset = now + timedelta(days=7)
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE, resets_at=reset)
        assert resolve_dispatch_harness().provider is AgentHarnessProvider.API_KEY

        task = _claimed_task()
        task.park(not_before=reset)

        recover_windows(reset)

        assert resolve_dispatch_harness().provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        task.refresh_from_db()
        assert task.not_before is None


class TestNoUsableMeteredCredential(CredentialPolicyCase):
    """Flipping the setting must never turn a quiesce into a terminal failure."""

    def _strip_api_key(self) -> None:
        self._monkeypatch.delenv(_API_KEY_VAR, raising=False)

    def test_the_lane_reads_unavailable_with_no_key_and_no_routing(self) -> None:
        self._strip_api_key()
        assert api_key_lane_available(scope="") is False

    def test_plans_first_stays_on_plans_when_the_api_is_unusable(self) -> None:
        self._strip_api_key()
        _pin(_COMPOSITE.value)
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        assert resolve_dispatch_harness().provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH

    def test_the_fallback_is_warned_never_silent(self) -> None:
        self._strip_api_key()
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        with self.assertLogs(_POLICY_LOGGER, level="WARNING") as logs:
            resolve_credential_provider(_COMPOSITE, scope="")
        assert len(logs.records) == 1
        assert "anthropic_api_key_pass_paths" in logs.output[0]


class TestCompositeNeverEscapes(CredentialPolicyCase):
    def test_dispatch_resolution_never_returns_the_composite(self) -> None:
        _pin(_COMPOSITE.value)
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                if exhausted:
                    _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
                assert resolve_dispatch_harness().provider is not _COMPOSITE

    def test_the_policy_itself_never_returns_the_composite(self) -> None:
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                if exhausted:
                    _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
                assert resolve_credential_provider(_COMPOSITE, scope="") is not _COMPOSITE

    def test_every_other_provider_resolves_unchanged(self) -> None:
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        for configured in (None, *(p for p in AgentHarnessProvider if p is not _COMPOSITE)):
            with self.subTest(configured=str(configured)):
                assert resolve_credential_provider(configured, scope="") is configured


class TestSystemChildEnv(CredentialPolicyCase):
    def test_a_system_spawn_rides_the_plan_until_exhaustion(self) -> None:
        _pin(_COMPOSITE.value)
        plan_env = system_child_env()
        assert plan_env is not None
        assert _OAUTH_VAR in plan_env
        assert _API_KEY_VAR not in plan_env

        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)

        metered_env = system_child_env()
        assert metered_env is not None
        assert _API_KEY_VAR in metered_env
        assert _OAUTH_VAR not in metered_env


class TestConfigSurface(CredentialPolicyCase):
    def test_the_composite_is_valid_under_claude_sdk_and_refused_under_pydantic_ai(self) -> None:
        assert check_harness_provider_pair("claude_sdk", _COMPOSITE.value) is None
        assert check_harness_provider_pair("pydantic_ai", _COMPOSITE.value) is not None

    def test_the_settings_surface_offers_the_composite(self) -> None:
        assert _COMPOSITE.value in setting_choices("agent_harness_provider")


class TestEvalLane(CredentialPolicyCase):
    def test_the_eval_lane_stays_on_plans_under_the_composite(self) -> None:
        _pin(_COMPOSITE.value)
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        assert isinstance(resolve_eval_credential(scope=""), AnthropicSubscriptionCredential)
