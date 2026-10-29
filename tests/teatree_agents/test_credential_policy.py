"""``teatree.agents.credential_policy`` — ride the plans, fail over to the meter, come back.

``agent_harness_provider=subscription_then_api_key`` is a POLICY, not a credential: it is
resolved ONCE into one of the two concrete ``claude_sdk`` providers, so every downstream
consumer (the lane map, the child env, ``t3 cost``) keeps seeing only values it already
handles. These tests pin the resolution, the exhaustion rule it reads, and the invariant
that the composite never escapes the policy.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from teatree.agents._runner_env import _provider_child_env, system_child_env
from teatree.agents.credential_policy import (
    ALL_ACCOUNTS_EXHAUSTED_CAUSE,
    EXHAUSTION_CAUSES,
    clear_stale_exhaustion_window,
    resolve_credential_provider,
    spend_metered_key,
    subscription_lane_exhausted,
)
from teatree.agents.harness import resolve_harness
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.runner import _resolve_dispatch_lane
from teatree.agents.usage_window import maybe_park_for_active_window, record_park
from teatree.config import AgentHarnessProvider
from teatree.config.cross_key_consistency import check_harness_provider_pair
from teatree.config.schema import setting_choices
from teatree.core.models import (
    LIMIT_PARKED_PREFIX,
    AnthropicActivePick,
    AnthropicTokenUsage,
    ConfigSetting,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    UsageWindowState,
)
from teatree.core.models.anthropic_token_usage import TokenHealthReading
from teatree.credential_config import api_key_lane_available, resolve_eval_credential
from teatree.llm.anthropic_limits import LimitCause
from teatree.llm.credentials import AnthropicSubscriptionCredential
from teatree.loops.usage_window_recovery import recover_windows
from tests.teatree_agents._metered_keys import KEY_1, KEY_2, SCOPE, cap_the_plans, route_metered_keys

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
    def test_admission_defers_live_health_probe_to_recovery(self) -> None:
        self._monkeypatch.delenv(_OAUTH_VAR, raising=False)
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/plan/oauth"])
        task = _claimed_task()

        with (
            patch("teatree.credential_refresh.AccountProber.read", return_value=None) as probe,
            patch("teatree.agents.credential_policy.oauth_lane_available", return_value=False),
        ):
            assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION) is not None
        probe.assert_not_called()

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

    def test_newly_available_oauth_account_overrides_an_old_all_accounts_window(self) -> None:
        window = _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/old/oauth"], scope="overlay-a")
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/new/oauth"], scope="overlay-b")
        task = _claimed_task()
        task.ticket.overlay = "overlay-a"
        task.ticket.save(update_fields=["overlay"])
        parked = Task.objects.create(
            ticket=task.ticket, session=task.session, phase="testing", status=Task.Status.CLAIMED
        )
        reset = window.resets_at
        assert reset is not None
        record_park(
            parked,
            reason=f"{LIMIT_PARKED_PREFIX}all configured subscription accounts exhausted",
            not_before=reset,
        )
        unrelated = Task.objects.create(
            ticket=task.ticket, session=task.session, phase="reviewing", status=Task.Status.CLAIMED
        )
        record_park(unrelated, reason=f"{LIMIT_PARKED_PREFIX}api_credit: credit spent", not_before=reset)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=ALL_ACCOUNTS_EXHAUSTED_CAUSE,
            resets_at=reset + timedelta(days=1),
        )
        window.refresh_from_db()
        reading = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.2,
            utilization_7d=0.2,
            status_5h="allowed",
            status_7d="allowed",
            reset_5h=None,
            reset_7d=None,
        )
        spent = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.99,
            utilization_7d=0.2,
            status_5h="rejected",
            status_7d="allowed",
            reset_5h=reset,
            reset_7d=None,
        )
        assert subscription_lane_exhausted() is False
        with (
            patch("teatree.credential_refresh.AccountProber.read", side_effect=(spent, reading)) as probe,
            patch("teatree.agents.credential_policy.ModeOverride.objects.clear_auto_engaged_token_outage") as clear,
        ):
            assert clear_stale_exhaustion_window(window, scope="overlay-a", now=timezone.now()) == 1
        assert [call.args[0] for call in probe.call_args_list] == ["anthropic/old/oauth", "anthropic/new/oauth"]
        clear.assert_called_once_with()
        window.refresh_from_db()
        parked.refresh_from_db()
        unrelated.refresh_from_db()
        assert window.cleared_at is not None
        assert parked.not_before is None
        assert unrelated.not_before == reset
        assert task.status == Task.Status.CLAIMED

    def test_missing_health_keeps_the_window_and_throttles_recovery_reprobe(self) -> None:
        self._monkeypatch.delenv(_OAUTH_VAR, raising=False)
        window = _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/unreadable/oauth"])
        task = _claimed_task()
        second = Task.objects.create(
            ticket=task.ticket, session=task.session, phase="testing", status=Task.Status.CLAIMED
        )
        with (
            patch("teatree.credential_refresh.AccountProber.read", return_value=None) as probe,
            patch("teatree.agents.credential_policy.oauth_lane_available", return_value=False),
        ):
            assert clear_stale_exhaustion_window(window, scope=None, now=timezone.now()) is None
            assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION) is not None
            assert maybe_park_for_active_window(second, lane=TaskAttempt.Lane.SUBSCRIPTION) is not None
            assert clear_stale_exhaustion_window(window, scope=None, now=timezone.now()) is None
        probe.assert_called_once_with("anthropic/unreadable/oauth")
        window.refresh_from_db()
        assert window.cleared_at is None

    def test_unhealthy_account_keeps_the_future_reset(self) -> None:
        window = _plan_window(LimitCause.SUBSCRIPTION_WEEKLY.value)
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/spent/oauth"])
        task = _claimed_task()
        reading = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.2,
            utilization_7d=0.99,
            status_5h="allowed",
            status_7d="rejected",
            reset_5h=None,
            reset_7d=window.resets_at,
        )
        with patch("teatree.credential_refresh.AccountProber.read", return_value=reading):
            assert clear_stale_exhaustion_window(window, scope=None, now=timezone.now()) is None
            attempt = maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert attempt is not None
        window.refresh_from_db()
        task.refresh_from_db()
        assert window.cleared_at is None
        assert task.not_before == window.resets_at

    def test_healthy_account_releases_an_admission_park_before_reset(self) -> None:
        window = _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/plan/oauth"])
        task = _claimed_task()
        reset = window.resets_at
        assert reset is not None
        spent = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.99,
            utilization_7d=0.2,
            status_5h="rejected",
            status_7d="allowed",
            reset_5h=reset,
            reset_7d=None,
        )
        healthy = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.2,
            utilization_7d=0.2,
            status_5h="allowed",
            status_7d="allowed",
            reset_5h=None,
            reset_7d=None,
        )
        with patch("teatree.credential_refresh.AccountProber.read", return_value=spent):
            assert clear_stale_exhaustion_window(window, scope=None, now=timezone.now()) is None
            attempt = maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert attempt is not None
        assert "window on lane 'subscription' active" in attempt.error
        legacy = Task.objects.create(
            ticket=task.ticket, session=task.session, phase="testing", status=Task.Status.CLAIMED
        )
        record_park(
            legacy,
            reason=(
                f"{LIMIT_PARKED_PREFIX}admission: all_accounts_exhausted "
                "window on lane TaskAttempt.Lane.SUBSCRIPTION active"
            ),
            not_before=reset,
        )
        cache.clear()
        with patch("teatree.credential_refresh.AccountProber.read", return_value=healthy):
            released = clear_stale_exhaustion_window(window, scope=None, now=timezone.now())
        task.refresh_from_db()
        legacy.refresh_from_db()
        window.refresh_from_db()
        assert released == 2
        assert task.not_before is None
        assert legacy.not_before is None
        assert window.cleared_at is not None

    def test_partial_reading_does_not_replace_known_exhaustion(self) -> None:
        window = _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        path = "anthropic/partial/oauth"
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", [path])
        spent = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.99,
            utilization_7d=0.2,
            status_5h="rejected",
            status_7d="allowed",
            reset_5h=window.resets_at,
            reset_7d=None,
        )
        cached = AnthropicTokenUsage.objects.record(path, spent, now=timezone.now())
        partial = TokenHealthReading(
            organization_id="org",
            utilization_5h=0.2,
            utilization_7d=None,
            status_5h="allowed",
            status_7d="",
            reset_5h=None,
            reset_7d=None,
        )
        task = _claimed_task()
        with patch("teatree.credential_refresh.AccountProber.read", return_value=partial):
            assert clear_stale_exhaustion_window(window, scope=None, now=timezone.now()) is None
            assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION) is not None
        cached.refresh_from_db()
        window.refresh_from_db()
        assert cached.is_exhausted
        assert window.cleared_at is None

    def test_failed_health_probe_names_window_and_keeps_reset(self) -> None:
        window = _plan_window(LimitCause.SUBSCRIPTION_SESSION.value)
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["anthropic/down/oauth"])
        task = _claimed_task()
        with (
            patch("teatree.credential_refresh.AccountProber.read", side_effect=RuntimeError("probe down")),
            self.assertLogs(_POLICY_LOGGER, level="WARNING") as log,
        ):
            assert clear_stale_exhaustion_window(window, scope=None, now=timezone.now()) is None
            attempt = maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert attempt is not None
        assert str(window.pk) in " ".join(log.output)
        window.refresh_from_db()
        assert window.cleared_at is None

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
    def test_non_claude_review_retry_keeps_unset_provider(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        task = _claimed_task()
        Task.objects.filter(pk=task.pk).update(phase="architectural_review", execution_reason="[plans-only-retry]")
        task.refresh_from_db()

        assert resolve_dispatch_harness(task).provider is None

    def test_unset_provider_review_retry_explicitly_selects_plans(self) -> None:
        assert resolve_credential_provider(None, scope="", plans_only=True) is AgentHarnessProvider.SUBSCRIPTION_OAUTH

    def test_architectural_review_retry_stays_on_plans_after_credit_refusal(self) -> None:
        _pin(_COMPOSITE.value)
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)
        task = _claimed_task()
        Task.objects.filter(pk=task.pk).update(phase="architectural_review", execution_reason="[plans-only-retry]")
        task.refresh_from_db()

        provider = resolve_dispatch_harness(task, phase=task.phase).provider
        assert provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        assert _resolve_dispatch_lane(resolve_harness(), provider) == TaskAttempt.Lane.SUBSCRIPTION
        child = _provider_child_env(provider).env
        assert child is not None
        assert _OAUTH_VAR in child
        assert _API_KEY_VAR not in child

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

    def test_an_all_accounts_window_over_spent_accounts_still_fails_over_to_the_api(self) -> None:
        _pin(_COMPOSITE.value)
        oauth_paths = ["anthropic/a/oauth", "anthropic/b/oauth"]
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", oauth_paths)
        for path in oauth_paths:
            AnthropicTokenUsage.objects.record(
                path,
                TokenHealthReading(
                    organization_id="org",
                    utilization_5h=0.99,
                    utilization_7d=0.1,
                    status_5h="allowed",
                    status_7d="allowed",
                    reset_5h=timezone.now() + timedelta(hours=2),
                    reset_7d=None,
                ),
                now=timezone.now(),
            )
        _plan_window(ALL_ACCOUNTS_EXHAUSTED_CAUSE)

        assert subscription_lane_exhausted() is True
        assert resolve_dispatch_harness().provider is AgentHarnessProvider.API_KEY

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


class TestSpendMeteredKey(CredentialPolicyCase):
    def setUp(self) -> None:
        self.moment = timezone.now()

    def _spend(self, signer: str, *, lane: str = TaskAttempt.Lane.METERED) -> datetime | None:
        return spend_metered_key(scope=SCOPE, lane=lane, signer=signer, moment=self.moment)

    def test_marks_the_signing_key_not_the_active_pick(self) -> None:
        route_metered_keys(self, [KEY_1, KEY_2])
        cap_the_plans()
        AnthropicActivePick.objects.set_pick("api_key", SCOPE, KEY_2)

        self._spend(KEY_1)

        assert list(AnthropicTokenUsage.objects.values_list("pass_path", flat=True)) == [KEY_1]

    def test_serves_now_while_another_key_resolves(self) -> None:
        route_metered_keys(self, [KEY_1, KEY_2])
        cap_the_plans()

        assert self._spend(KEY_1) == self.moment

    def test_waits_for_the_plan_reset_when_no_key_is_left(self) -> None:
        route_metered_keys(self, [KEY_1])
        reset = cap_the_plans()

        assert self._spend(KEY_1) == reset

    def test_serves_now_on_the_plans_when_no_window_is_open(self) -> None:
        route_metered_keys(self, [KEY_1])

        assert self._spend(KEY_1) == self.moment
        assert AnthropicTokenUsage.objects.get(pass_path=KEY_1).is_exhausted

    def test_an_ambient_key_waits_for_the_plan_reset_and_marks_nothing(self) -> None:
        route_metered_keys(self, [])
        reset = cap_the_plans()

        assert self._spend("") == reset
        assert not AnthropicTokenUsage.objects.exists()

    def test_no_key_and_no_plan_window_is_left_alone(self) -> None:
        route_metered_keys(self, [])

        assert self._spend("") is None

    def test_only_the_composite_on_the_metered_lane_is_touched(self) -> None:
        for provider, lane in (
            (AgentHarnessProvider.SUBSCRIPTION_OAUTH.value, TaskAttempt.Lane.METERED),
            (AgentHarnessProvider.API_KEY.value, TaskAttempt.Lane.METERED),
            (_COMPOSITE.value, TaskAttempt.Lane.SUBSCRIPTION),
        ):
            with self.subTest(provider=provider, lane=lane):
                route_metered_keys(self, [KEY_1, KEY_2], provider=provider)
                cap_the_plans()

                assert self._spend(KEY_1, lane=lane) is None
                assert not AnthropicTokenUsage.objects.exists()
