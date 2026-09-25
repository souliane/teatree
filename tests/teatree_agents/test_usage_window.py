"""``teatree.agents.usage_window`` — park-not-fail + admission guard (Directive #3).

A limit parks the task and records the window, so the recovery chain can re-arm it at
reset — permanently, with no flag to turn it off.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import django.test
import pytest
from django.utils import timezone
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel

import teatree.agents.runner as runner_mod
from teatree.agents.attempt_recorder import AttemptUsage
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.usage_window import (
    ELAPSED_RESET_GRACE,
    LimitSignal,
    effective_resets_at,
    maybe_park_for_active_window,
    park_or_rotate_on_limit,
    park_task_on_all_exhausted,
    park_task_on_limit,
)
from teatree.core.modelkit.task_failure_taxonomy import RecoveryStrategy, classify_failure, recovery_strategy
from teatree.core.models import (
    LIMIT_PARKED_PREFIX,
    AnthropicActivePick,
    AnthropicTokenUsage,
    BotPing,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    UsageWindowState,
)
from teatree.core.models.anthropic_token_usage import REJECTED_STATUS, TokenHealthReading
from teatree.core.models.config_setting import ConfigSetting
from teatree.llm.anthropic_limits import LimitCause, LimitMatch
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import fake_sdk, result_message


def _claimed_task() -> Task:
    ticket = Ticket.objects.create(issue_url="https://example.com/i/1", role=Ticket.Role.AUTHOR)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        phase="coding",
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED)
    task.refresh_from_db()
    return task


_SESSION_MATCH = LimitMatch(phrase="five_hour", cause=LimitCause.SUBSCRIPTION_SESSION)
_WEEKLY_MATCH = LimitMatch(phrase="seven_day", cause=LimitCause.SUBSCRIPTION_WEEKLY)
_CREDIT_MATCH = LimitMatch(phrase="out_of_credits", cause=LimitCause.API_CREDIT)
_RATE_LIMIT_MATCH = LimitMatch(phrase="rate limit", cause=LimitCause.RATE_LIMIT)
_BUDGET_MATCH = LimitMatch(phrase="insufficient_user_quota", cause=LimitCause.PROVIDER_BUDGET)
_OWNER_ALERT = "teatree.agents.usage_window.notify_user"

_OAUTH_SETTING = "anthropic_oauth_pass_paths"
_USAGE = AttemptUsage(input_tokens=4200, output_tokens=310, cost_usd=0.42)


def _seed_healthy(pass_path: str) -> None:
    """Cache *pass_path* as a fresh, healthy account so the selector routes to it with no probe."""
    AnthropicTokenUsage.objects.record(
        pass_path,
        TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.1,
            utilization_7d=0.1,
            status_5h="allowed",
            status_7d="allowed",
            reset_5h=None,
            reset_7d=None,
        ),
        now=timezone.now(),
    )


def _seed_exhausted(pass_path: str, *, reset: datetime) -> None:
    """Cache *pass_path* as a fresh, exhausted account (7d rejected until *reset*)."""
    AnthropicTokenUsage.objects.record(
        pass_path,
        TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.1,
            utilization_7d=1.0,
            status_5h="allowed",
            status_7d=REJECTED_STATUS,
            reset_5h=None,
            reset_7d=reset,
        ),
        now=timezone.now(),
    )


class TestEffectiveResetsAt(django.test.SimpleTestCase):
    def test_sdk_reset_wins(self) -> None:
        now = timezone.now()
        sdk = now + timedelta(hours=2)
        assert effective_resets_at(LimitCause.SUBSCRIPTION_SESSION, sdk, now) == sdk

    def test_session_horizon_fallback(self) -> None:
        now = timezone.now()
        assert effective_resets_at(LimitCause.SUBSCRIPTION_SESSION, None, now) == now + timedelta(hours=5)

    def test_credit_has_no_reset(self) -> None:
        now = timezone.now()
        assert effective_resets_at(LimitCause.API_CREDIT, None, now) is None

    def test_credit_ignores_an_sdk_reset(self) -> None:
        # An `overage` rejection maps to API_CREDIT yet can carry a top-level resets_at;
        # the cause has no time-based recovery, so the SDK value must NOT re-arm it.
        now = timezone.now()
        assert effective_resets_at(LimitCause.API_CREDIT, now + timedelta(hours=1), now) is None


class TestParkTaskOnLimit(django.test.TestCase):
    def test_session_limit_parks_task_and_records_window(self) -> None:
        now = timezone.now()
        task = _claimed_task()
        parked = park_task_on_limit(task, _SESSION_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        assert parked is not None
        assert parked.error.startswith(LIMIT_PARKED_PREFIX)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING  # parked, NOT terminal FAILED
        assert task.not_before == now + timedelta(hours=5)
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.cause == LimitCause.SUBSCRIPTION_SESSION.value
        assert window.resets_at == now + timedelta(hours=5)

    def test_sdk_reset_timestamp_is_used(self) -> None:
        now = timezone.now()
        sdk_epoch = int((now + timedelta(hours=3)).timestamp())
        task = _claimed_task()
        park_task_on_limit(
            task,
            _SESSION_MATCH,
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
            signal=LimitSignal(sdk_resets_at=sdk_epoch),
        )
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == datetime.fromtimestamp(sdk_epoch, tz=UTC)

    def test_unparseable_sdk_epoch_falls_back_to_horizon(self) -> None:
        # A garbage/overflowing SDK resets_at is ignored — the cause horizon fills the gap.
        now = timezone.now()
        task = _claimed_task()
        park_task_on_limit(
            task, _SESSION_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now, signal=LimitSignal(sdk_resets_at=10**20)
        )
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == now + timedelta(hours=5)

    def test_credit_exhaustion_is_not_parked(self) -> None:
        # API-credit has no time-based recovery → nothing to re-arm to, so it stays a
        # terminal FAILED (the caller's fallback), never an indefinitely-parked task.
        task = _claimed_task()
        parked = park_task_on_limit(task, _CREDIT_MATCH, lane=TaskAttempt.Lane.METERED)
        assert parked is None
        assert not UsageWindowState.objects.exists()

    def test_credit_with_an_sdk_overage_reset_is_still_not_parked(self) -> None:
        # An `overage` rejection maps to API_CREDIT but carries a top-level resets_at. Parking
        # it would spin the credit-exhausted lane (recover → re-dispatch → re-park) and never
        # tell the operator to add credits — so it stays a terminal FAILED, not a park.
        now = timezone.now()
        sdk_epoch = int((now + timedelta(hours=1)).timestamp())
        task = _claimed_task()
        parked = park_task_on_limit(
            task, _CREDIT_MATCH, lane=TaskAttempt.Lane.METERED, signal=LimitSignal(sdk_resets_at=sdk_epoch)
        )
        assert parked is None
        assert not UsageWindowState.objects.exists()
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED  # untouched — caller records the terminal FAILED
        assert task.not_before is None

    def test_parked_attempt_excluded_from_repair_budget(self) -> None:
        from teatree.core.models.task_repair import phase_attempts  # noqa: PLC0415 — deferred (test-local)

        task = _claimed_task()
        park_task_on_limit(task, _SESSION_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert phase_attempts(task) == []

    def test_a_reactive_park_records_the_usage_the_triggering_result_billed(self) -> None:
        """A limit reported ON a result is POST-turn — the park must not discard its spend (#4164)."""
        task = _claimed_task()
        parked = park_task_on_limit(
            task, _SESSION_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, signal=LimitSignal(usage=_USAGE)
        )
        assert parked is not None
        assert parked.input_tokens == 4200
        assert parked.output_tokens == 310
        assert parked.cost_usd == pytest.approx(0.42)

    def test_no_usage_still_records_null_not_zero(self) -> None:
        """The default stays NULL for a genuinely pre-dispatch caller — no false measurement."""
        task = _claimed_task()
        parked = park_task_on_limit(task, _SESSION_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert parked is not None
        assert parked.input_tokens is None


def _cycle_stop_model(reset: datetime, provider_calls: list[str]) -> FunctionModel:
    """A metered router refusing every request on its key's monthly spend cap, as OrcaRouter words it."""

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
        await asyncio.sleep(0)
        provider_calls.append("chat/completions")
        raise ModelHTTPError(
            status_code=403,
            model_name="deepseek/deepseek-v4.1-flash",
            body={
                "error": {
                    "message": f"token cycle spend limit reached, resets at {reset:%Y-%m-%dT%H:%M:%SZ}",
                    "type": "access_denied",
                }
            },
        )
        yield ""  # unreachable — makes this an async generator

    return FunctionModel(stream_function=stream_fn)


class TestProviderBudgetParksTheMeteredLane(django.test.TestCase):
    """A metered router's spend stop parks the whole lane once instead of every queued task hitting it."""

    def test_the_first_stop_parks_the_lane_and_the_next_task_never_reaches_the_provider(self) -> None:
        reset = (timezone.now() + timedelta(days=3)).replace(microsecond=0)
        provider_calls: list[str] = []
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        first, second = (Task.objects.create(ticket=ticket, session=session, phase="coding") for _ in range(2))

        with (
            patch.object(
                runner_mod,
                "resolve_dispatch_harness",
                return_value=runner_mod.DispatchHarness(
                    harness=PydanticAiHarness(model=_cycle_stop_model(reset, provider_calls)),
                    name="fake_harness",
                    provider=None,
                ),
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
            patch(_OWNER_ALERT, return_value=True) as owner_alert,
        ):
            stopped = run_agent(first, phase="coding", overlay_skill_metadata={})
            admitted = run_agent(second, phase="coding", overlay_skill_metadata={})

        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED)
        assert window is not None
        assert window.cause == LimitCause.PROVIDER_BUDGET.value
        assert window.resets_at == reset, "parked until the reset the router stated, not a guessed horizon"
        assert stopped.error.startswith(f"{LIMIT_PARKED_PREFIX}provider_budget: token cycle spend limit reached")
        assert admitted.error.startswith(f"{LIMIT_PARKED_PREFIX}admission: provider_budget window")
        for task in (first, second):
            task.refresh_from_db()
            assert task.status == Task.Status.PENDING, "parked, never FAILED into a retry"
            assert task.not_before == reset
        assert provider_calls == ["chat/completions"], "the second task was parked without a provider call"
        owner_alert.assert_called_once()
        assert str(window.pk) in owner_alert.call_args.kwargs["idempotency_key"]

    def test_a_stop_stating_no_reset_is_re_probed_after_six_hours(self) -> None:
        now = timezone.now()
        task = _claimed_task()
        with patch(_OWNER_ALERT, return_value=True):
            park_task_on_limit(task, _BUDGET_MATCH, lane=TaskAttempt.Lane.METERED, now=now)
        task.refresh_from_db()
        assert task.not_before == now + timedelta(hours=6)

    def test_every_park_behind_one_window_shares_one_owner_alert_key(self) -> None:
        first = _claimed_task()
        second = Task.objects.create(ticket=first.ticket, session=first.session, phase="coding")
        with patch(_OWNER_ALERT, return_value=True) as owner_alert:
            for task in (first, second):
                park_task_on_limit(task, _BUDGET_MATCH, lane=TaskAttempt.Lane.METERED)
        keys = {call.kwargs["idempotency_key"] for call in owner_alert.call_args_list}
        assert len(keys) == 1, "the notify ledger dedupes on this key, so the owner hears once per window"

    def test_a_rate_limit_park_does_not_alert_the_owner(self) -> None:
        with patch(_OWNER_ALERT, return_value=True) as owner_alert:
            park_task_on_limit(_claimed_task(), _RATE_LIMIT_MATCH, lane=TaskAttempt.Lane.METERED)
        owner_alert.assert_not_called()

    def test_a_failed_owner_alert_never_breaks_the_park(self) -> None:
        task = _claimed_task()
        with patch(_OWNER_ALERT, side_effect=RuntimeError("slack down")):
            parked = park_task_on_limit(task, _BUDGET_MATCH, lane=TaskAttempt.Lane.METERED)
        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING


def _refusing_model(body: dict[str, object], provider_calls: list[str]) -> FunctionModel:
    """A metered router refusing the request outright with HTTP 400 and *body*."""

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
        await asyncio.sleep(0)
        provider_calls.append("chat/completions")
        raise ModelHTTPError(status_code=400, model_name="deepseek/deepseek-v4.1-flash", body=body)
        yield ""  # unreachable — makes this an async generator

    return FunctionModel(stream_function=stream_fn)


_GUARDRAIL_BLOCK = {
    "error": {
        "code": "guardrail_blocked",
        "message": "blocked by guardrail teatree-egress (rule secrets)",
        "type": "invalid_request_error",
    }
}


class TestALeakBlockIsTerminal(django.test.TestCase):
    """A guardrail block fails the task once, alerts the owner once, and is never re-sent."""

    def _dispatch_refused_with(self, body: dict[str, object]) -> tuple[Task, TaskAttempt, list[str]]:
        provider_calls: list[str] = []
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ticket = planned_ticket()
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="agent-1"), phase="coding"
        )
        with (
            patch.object(
                runner_mod,
                "resolve_dispatch_harness",
                return_value=runner_mod.DispatchHarness(
                    harness=PydanticAiHarness(model=_refusing_model(body, provider_calls)),
                    name="fake_harness",
                    provider=None,
                ),
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(task, phase="coding", overlay_skill_metadata={})
        task.refresh_from_db()
        return task, attempt, provider_calls

    def test_a_guardrail_block_fails_terminally_with_exactly_one_owner_alert(self) -> None:
        task, attempt, provider_calls = self._dispatch_refused_with(_GUARDRAIL_BLOCK)

        assert task.status == Task.Status.FAILED, "a leak block is never parked for a retry"
        assert attempt.error.startswith("leak_blocked: guardrail_blocked")
        assert recovery_strategy(classify_failure(attempt.error)) is RecoveryStrategy.HALT
        assert provider_calls == ["chat/completions"], "the blocked context is sent once and never again"
        assert not UsageWindowState.objects.exists(), "a content block is not a lane-wide window"
        assert BotPing.objects.filter(idempotency_key__startswith="leak_blocked:").count() == 1

    def test_the_same_400_without_the_block_code_stays_an_ordinary_failure(self) -> None:
        body = {"error": {"code": "invalid_request", "message": "bad request", "type": "invalid_request_error"}}

        _task, attempt, _calls = self._dispatch_refused_with(body)

        assert attempt.error.startswith("result_error: ")
        assert not BotPing.objects.filter(idempotency_key__startswith="leak_blocked:").exists()


class TestHttpStatusParksOnlyTheMeteredTransport(django.test.TestCase):
    """A bare 402/429 parks a lane only when the metered router said it, never on a claude_sdk result."""

    def _task(self) -> Task:
        ticket = planned_ticket()
        return Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="agent-1"), phase="coding"
        )

    def _claude_sdk_refused_with(self, status: int) -> TaskAttempt:
        refusal = result_message(
            is_error=True,
            subtype="error_during_execution",
            result=f"status {status}: the request could not be completed",
            api_error_status=status,
        )
        task = self._task()
        with fake_sdk([refusal]):
            return run_agent(task, phase="coding", overlay_skill_metadata={})

    def test_a_claude_sdk_402_parks_no_lane(self) -> None:
        attempt = self._claude_sdk_refused_with(402)

        assert attempt.error.startswith("result_error: ")
        assert not UsageWindowState.objects.exists(), "a subscription run is never parked as a router budget"

    def test_a_claude_sdk_429_without_wording_parks_no_lane(self) -> None:
        attempt = self._claude_sdk_refused_with(429)

        assert attempt.error.startswith("result_error: ")
        assert not UsageWindowState.objects.exists()

    def test_a_metered_402_still_parks_the_metered_lane_as_a_budget(self) -> None:
        provider_calls: list[str] = []
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        task = self._task()

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            await asyncio.sleep(0)
            provider_calls.append("chat/completions")
            raise ModelHTTPError(status_code=402, model_name="m", body={"error": {"message": "payment required"}})
            yield ""  # unreachable — makes this an async generator

        harness = PydanticAiHarness(model=FunctionModel(stream_function=stream_fn))
        with (
            patch.object(
                runner_mod,
                "resolve_dispatch_harness",
                return_value=runner_mod.DispatchHarness(harness=harness, name="fake_harness", provider=None),
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
            patch(_OWNER_ALERT, return_value=True),
        ):
            attempt = run_agent(task, phase="coding", overlay_skill_metadata={})

        assert attempt.error.startswith(f"{LIMIT_PARKED_PREFIX}provider_budget: http 402")
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED)
        assert window is not None
        assert window.cause == LimitCause.PROVIDER_BUDGET.value


class TestAdmissionGuard(django.test.TestCase):
    def test_parks_dispatch_while_window_active(self) -> None:
        now = timezone.now()
        reset = now + timedelta(hours=5)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=reset,
            now=now,
        )
        task = _claimed_task()
        parked = maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == reset

    def test_no_window_lets_dispatch_through(self) -> None:
        task = _claimed_task()
        assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION) is None

    def test_due_window_lets_dispatch_through(self) -> None:
        # The window's reset already passed (recovery will clear it) — let the dispatch try.
        now = timezone.now()
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now - timedelta(minutes=1),
            now=now - timedelta(hours=5),
        )
        task = _claimed_task()
        assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now) is None


class TestParkOrRotateOnLimit(django.test.TestCase):
    """Multi-account #C1: a mid-run subscription limit ROTATES to a healthy account before parking.

    One account hitting its 5h/weekly window must NOT park the whole subscription lane while
    other accounts are healthy — that stranded the healthy accounts. The reactive handler
    records the current account exhausted and re-consults the selector: another healthy account
    → REQUEUE (rotate, no lane park); every account spent → park the lane for auto-resume.
    """

    def test_rotates_to_a_healthy_account_without_parking_the_lane(self) -> None:
        # account-1 hit its 5h limit mid-run; account-2 is healthy → the next dispatch must
        # route to account-2 and NO usage-window (lane park) is recorded.
        now = timezone.now()
        reset = now + timedelta(hours=3)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth", "acct/3/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")  # the account that hit the limit
        _seed_healthy("acct/2/oauth")
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task,
            _SESSION_MATCH,
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
            signal=LimitSignal(sdk_resets_at=int(reset.timestamp()), usage=_USAGE),
        )

        assert parked is not None, "the task is requeued (an audit attempt is recorded), never failed"
        assert parked.error.startswith(LIMIT_PARKED_PREFIX)
        assert parked.input_tokens == 4200, (
            "the rotation park is POST-turn too — it billed on the account that hit its limit"
        )
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "requeued to rotate, NOT terminal FAILED"
        assert task.not_before is not None
        assert task.not_before <= now, "claimable immediately on the next tick"
        assert not UsageWindowState.objects.exists(), "no lane park while a healthy account remains — C1"
        assert AnthropicActivePick.objects.pick_for("oauth", "") == "acct/2/oauth", "the sticky pick rotated"
        exhausted = AnthropicTokenUsage.objects.get(pass_path="acct/1/oauth")
        assert exhausted.is_exhausted, "the spent account is recorded exhausted so the selector routes off it"

    def test_all_accounts_exhausted_parks_the_lane_keyed_on_earliest_reset(self) -> None:
        # account-1 hits its limit and the other accounts are ALREADY exhausted → there is no
        # account to rotate to, so the whole lane parks (auto-resume at the earliest reset).
        now = timezone.now()
        soon = now + timedelta(hours=1)
        later = now + timedelta(hours=4)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")
        _seed_exhausted("acct/2/oauth", reset=soon)
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task,
            _WEEKLY_MATCH,
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
            # account-1's own reset — later than account-2's
            signal=LimitSignal(sdk_resets_at=int(later.timestamp()), usage=_USAGE),
        )

        assert parked is not None
        assert parked.input_tokens == 4200, "reached via the rotation path's all-exhausted fallback — still POST-turn"
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "parked, not FAILED"
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None, "all accounts spent → the whole lane parks"
        # parked until the EARLIEST account frees up, not account-1's later reset
        assert window.resets_at == soon
        assert task.not_before == soon

    def test_single_account_still_parks_the_lane_as_today(self) -> None:
        # No regression for the single-account deployment: its only account hitting the limit
        # has nowhere to rotate, so it parks the lane exactly like the pre-rotation behaviour.
        now = timezone.now()
        reset = (now + timedelta(hours=5)).replace(microsecond=0)  # epoch round-trips whole seconds
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["only/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "only/oauth")
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task,
            _SESSION_MATCH,
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
            signal=LimitSignal(sdk_resets_at=int(reset.timestamp())),
        )

        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == reset
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == reset

    def test_transient_rate_limit_parks_the_lane_without_rotating(self) -> None:
        # A transient 429 is lane-wide, not account-specific — it parks the lane (5-min horizon)
        # and never consults the per-account selector, so no account is marked exhausted.
        now = timezone.now()
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")
        task = _claimed_task()

        parked = park_or_rotate_on_limit(task, _RATE_LIMIT_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        assert parked is not None
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.cause == LimitCause.RATE_LIMIT.value
        # no account rotation for a lane-wide 429
        assert not AnthropicTokenUsage.objects.filter(pass_path="acct/1/oauth").exists()


class TestParkTaskOnAllExhausted(django.test.TestCase):
    """Multi-account #C2: every account drained → PARK the lane for auto-resume, never a human ping."""

    def test_parks_the_lane_keyed_on_the_earliest_reset(self) -> None:
        now = timezone.now()
        reset = now + timedelta(hours=2)
        task = _claimed_task()
        parked = park_task_on_all_exhausted(task, resets_at=reset, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        assert parked is not None
        assert parked.error.startswith(LIMIT_PARKED_PREFIX)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "PARKED, not FAILED — no human escalation"
        assert task.not_before == reset
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == reset

    def test_usage_defaults_to_null_for_the_pre_dispatch_caller(self) -> None:
        """runner.py's own call site is a CredentialError before any turn ran — no usage passed."""
        task = _claimed_task()
        parked = park_task_on_all_exhausted(
            task, resets_at=timezone.now() + timedelta(hours=1), lane=TaskAttempt.Lane.SUBSCRIPTION
        )
        assert parked is not None
        assert parked.input_tokens is None

    def test_usage_is_recorded_when_the_caller_supplies_it(self) -> None:
        """Reached POST-turn via the rotation path's all-exhausted fallback — carries real spend."""
        task = _claimed_task()
        parked = park_task_on_all_exhausted(
            task, resets_at=timezone.now() + timedelta(hours=1), lane=TaskAttempt.Lane.SUBSCRIPTION, usage=_USAGE
        )
        assert parked is not None
        assert parked.input_tokens == 4200

    def test_no_reset_is_not_parked(self) -> None:
        task = _claimed_task()
        assert park_task_on_all_exhausted(task, resets_at=None, lane="subscription") is None
        assert not UsageWindowState.objects.exists()

    def test_an_already_elapsed_reset_is_clamped_forward_not_refused(self) -> None:
        """A past reset still PARKS — clamped ahead — because refusing costs SEVEN DAYS.

        Parking on a past instant would be dead on arrival (the recovery chain clears it on
        its very next tick and re-parks at the poll cadence). But refusing outright is worse:
        the caller records a terminal FAILED whose ``all tokens exhausted`` signature maps to
        ``SUBSCRIPTION_WEEKLY``, so the transient-requeue horizon becomes 7 days for what is
        normally a minutes-long outage artefact.
        """
        now = timezone.now()
        task = _claimed_task()

        parked = park_task_on_all_exhausted(
            task, resets_at=now - timedelta(seconds=1), lane=TaskAttempt.Lane.SUBSCRIPTION, now=now
        )

        assert parked is not None, "still parked — a stale reset must not become a terminal FAILED"
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == now + ELAPSED_RESET_GRACE, "clamped to the grace horizon"
        assert not window.should_clear(now), "keyed in the FUTURE, so no instant self-clear + DM"
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "quiesced, not failed"

    def test_a_future_reset_is_left_untouched(self) -> None:
        now = timezone.now()
        reset = now + timedelta(hours=3)
        task = _claimed_task()

        park_task_on_all_exhausted(task, resets_at=reset, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == reset, "a real future reset is never clamped"


class TestImmediateRequeueOnFailover(django.test.TestCase):
    """Plans-first turns an exhaustion park into a rotation: requeue NOW, on the meter.

    Rotating across credential KINDS is the same event as rotating across accounts, so the
    task is returned to the queue at the park moment and the next tick re-dispatches it —
    on the API, because the window written a moment earlier is what makes the policy answer
    ``api_key``. The window row still carries the REAL reset, which is what later returns
    the lane to plans.
    """

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self._monkeypatch = monkeypatch

    @staticmethod
    def _pin(provider: str) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", provider)

    def test_a_plan_exhaustion_park_requeues_at_the_park_moment(self) -> None:
        self._pin("subscription_then_api_key")
        now = timezone.now()
        task = _claimed_task()

        park_task_on_limit(task, _WEEKLY_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        task.refresh_from_db()
        assert task.not_before == now, "claimable on the next tick, so the meter picks it up"
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == now + timedelta(days=7), "the window keeps the REAL reset"

    def test_an_all_exhausted_park_requeues_at_the_park_moment(self) -> None:
        self._pin("subscription_then_api_key")
        now = timezone.now()
        reset = now + timedelta(days=7)
        task = _claimed_task()

        park_task_on_all_exhausted(task, resets_at=reset, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        task.refresh_from_db()
        assert task.not_before == now
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == reset

    def test_a_transient_rate_limit_park_still_waits_out_its_window(self) -> None:
        self._pin("subscription_then_api_key")
        now = timezone.now()
        task = _claimed_task()

        park_task_on_limit(task, _RATE_LIMIT_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        task.refresh_from_db()
        assert task.not_before == now + timedelta(minutes=5), "a throttle is waited out, never hammered"

    def test_plans_only_still_parks_until_the_window_re_arms(self) -> None:
        self._pin("subscription_oauth")
        now = timezone.now()
        task = _claimed_task()

        park_task_on_limit(task, _WEEKLY_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        task.refresh_from_db()
        assert task.not_before == now + timedelta(days=7)

    def test_plans_first_with_no_usable_meter_parks_until_the_window_re_arms(self) -> None:
        self._pin("subscription_then_api_key")
        self._monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        now = timezone.now()
        task = _claimed_task()

        park_task_on_limit(task, _WEEKLY_MATCH, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)

        task.refresh_from_db()
        assert task.not_before == now + timedelta(days=7), "no meter to fail over to — quiesce, exactly as today"
