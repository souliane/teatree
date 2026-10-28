"""A routed candidate refused for credit has spent its key, not the route: rotate, else wait for the plans."""

import contextlib
import datetime as dt
import os
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

from claude_agent_sdk.types import RateLimitInfo
from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness as harness_mod
from teatree.agents.runner import run_agent
from teatree.agents.runner_route_recording import RouteFailureRecord
from teatree.agents.runner_stream import HarnessOutcome
from teatree.core.models import (
    LIMIT_PARKED_PREFIX,
    AgentRouteAvailability,
    AnthropicActivePick,
    AnthropicTokenUsage,
    ConfigSetting,
    Session,
    Task,
    TaskAttempt,
)
from teatree.core.models.anthropic_token_usage import TokenHealthReading
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._metered_keys import KEY_1, KEY_2, cap_the_plans, route_metered_keys
from tests.teatree_agents._route_fakes import CLAUDE_LIKE, CLAUDE_SDK, register_stub_harnesses, route_config, routed_by
from tests.teatree_agents._sdk_fake import (
    FakeHarnessSession,
    assistant_tool_use,
    fake_sdk,
    result_message,
    success_stream,
)

_SKILL = "ac-python"
_REFUSAL = result_message(
    subtype="error_during_execution", is_error=True, result="The request failed: you are out of credits."
)
_SUCCESS = success_stream({"summary": "done", "files_modified": [{"path": "src/x.py", "action": "modified"}]})
_SESSION_REFUSAL = result_message(
    subtype="error_during_execution", is_error=True, result="You've hit your five-hour limit."
)


def test_route_hold_uses_the_reported_reset() -> None:
    reset = timezone.now() + dt.timedelta(hours=3)
    outcome = HarnessOutcome(
        agent_text="",
        result_message=_SESSION_REFUSAL,
        stuck_reason=None,
        rate_limit_info=RateLimitInfo(status="rejected", rate_limit_type="five_hour", resets_at=int(reset.timestamp())),
    )

    hold = RouteFailureRecord(reason="subscription_session: limit", skills=[], outcome=outcome).hold

    assert hold is not None
    assert dt.timedelta(hours=2, minutes=59) < hold < dt.timedelta(hours=3, minutes=1)


def test_access_denial_ignores_a_key_lifetime_as_its_reset() -> None:
    expiration = timezone.now() + dt.timedelta(days=365)
    outcome = HarnessOutcome(
        agent_text="",
        result_message=result_message(subtype="error_during_execution", is_error=True, api_error_status=403),
        stuck_reason=None,
        rate_limit_info=RateLimitInfo(
            status="rejected", rate_limit_type="five_hour", resets_at=int(expiration.timestamp())
        ),
    )

    assert RouteFailureRecord(reason="http 403", skills=[], outcome=outcome).hold == dt.timedelta(hours=1)


@contextlib.contextmanager
def scripted_sdk(*runs: list[Any]) -> Generator[list[str]]:
    """Each SDK client the harness builds plays the next run (the last one repeats); yields the key each signed with."""
    signed: list[str] = []
    with fake_sdk(runs[0]):

        def make_client(*, options: Any = None, **_: object) -> FakeHarnessSession:
            signed.append(options.env.get("ANTHROPIC_API_KEY", ""))
            return FakeHarnessSession(runs[min(len(signed), len(runs)) - 1])

        with patch.object(harness_mod, "ClaudeSDKClient", make_client):
            yield signed


class TestRoutedMeteredCreditRefusal(TestCase):
    def setUp(self) -> None:
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def _run(self, *runs: list[Any], candidates: tuple[str, ...] = (CLAUDE_SDK,)) -> tuple[TaskAttempt, list[str]]:
        with (
            routed_by(route_config(_SKILL, *candidates)),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=[_SKILL]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            scripted_sdk(*runs) as signed,
        ):
            attempt = run_agent(self.task, phase="coding", overlay_skill_metadata=SkillMetadata())
        self.task.refresh_from_db()
        return attempt, signed

    def test_the_refused_key_rotates_to_the_second_key_with_no_route_hold(self) -> None:
        route_metered_keys(self, [KEY_1, KEY_2])
        cap_the_plans()

        attempt, signed = self._run([_REFUSAL], _SUCCESS)

        assert signed == [KEY_1, KEY_2]
        assert AnthropicTokenUsage.objects.get(pass_path=KEY_1).is_exhausted
        assert not AgentRouteAvailability.objects.exists()
        assert attempt.exit_code == 0
        assert list(self.task.attempts.order_by("pk").values_list("account", flat=True)) == [KEY_1, KEY_2]

    def test_every_key_spent_parks_until_the_plan_reset_outside_the_route_hold_cap(self) -> None:
        route_metered_keys(self, [KEY_1, KEY_2])
        reset = cap_the_plans()

        for _ in range(3):
            self._run([_REFUSAL])

        errors = list(self.task.attempts.values_list("error", flat=True))
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before == reset
        assert not [error for error in errors if error.startswith(f"{LIMIT_PARKED_PREFIX}route hold")]
        assert not AgentRouteAvailability.objects.exists()

    def test_an_ambient_key_parks_without_re_dispatching(self) -> None:
        route_metered_keys(self, [])
        self.enterContext(patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ambient-metered-key"}))
        reset = cap_the_plans()

        _, signed = self._run([_REFUSAL], _SUCCESS)

        assert signed == ["ambient-metered-key"]
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before == reset
        assert not AnthropicTokenUsage.objects.exists()
        assert not AgentRouteAvailability.objects.exists()

    def test_a_run_that_made_tool_calls_parks_instead_of_re_dispatching(self) -> None:
        route_metered_keys(self, [KEY_1, KEY_2])
        cap_the_plans()

        _, signed = self._run([assistant_tool_use(), _REFUSAL], _SUCCESS)

        assert signed == [KEY_1]
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before is not None
        assert self.task.not_before <= timezone.now()
        assert AnthropicTokenUsage.objects.get(pass_path=KEY_1).is_exhausted
        assert not AgentRouteAvailability.objects.exists()

    def test_a_later_route_candidate_is_tried_before_parking(self) -> None:
        register_stub_harnesses(self, CLAUDE_LIKE)
        route_metered_keys(self, [KEY_1])
        cap_the_plans()

        attempt, signed = self._run([_REFUSAL], candidates=(CLAUDE_SDK, CLAUDE_LIKE))

        assert signed == [KEY_1]
        assert (attempt.selected_harness, attempt.route_candidate_index) == (CLAUDE_LIKE, 1)
        assert not AgentRouteAvailability.objects.exists()

    def test_an_ambient_key_with_a_later_candidate_still_parks_without_a_route_hold(self) -> None:
        register_stub_harnesses(self, CLAUDE_LIKE)
        route_metered_keys(self, [])
        self.enterContext(patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ambient-metered-key"}))
        reset = cap_the_plans()

        attempt, signed = self._run([_REFUSAL], candidates=(CLAUDE_SDK, CLAUDE_LIKE))

        assert signed == ["ambient-metered-key"]
        assert (attempt.selected_harness, attempt.route_candidate_index) == (CLAUDE_SDK, 0)
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before == reset
        assert not AgentRouteAvailability.objects.exists()

    def test_another_provider_still_holds_the_route(self) -> None:
        route_metered_keys(self, [KEY_1], provider="api_key")

        self._run([_REFUSAL])

        assert AgentRouteAvailability.objects.filter(harness=CLAUDE_SDK).exists()
        assert not AnthropicTokenUsage.objects.exists()
        assert self.task.status == Task.Status.PENDING
        assert self.task.attempts.order_by("pk").first().account == KEY_1


class TestRoutedSubscriptionLimit(TestCase):
    def test_session_limit_rotates_account_without_holding_the_route(self) -> None:
        first = "anthropic/first/oauth"
        second = "anthropic/second/oauth"
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", [first, second])
        AnthropicActivePick.objects.set_pick("oauth", "", first)
        for path in (first, second):
            AnthropicTokenUsage.objects.record(
                path,
                TokenHealthReading(
                    organization_id="org",
                    utilization_5h=0.1,
                    utilization_7d=0.1,
                    status_5h="allowed",
                    status_7d="allowed",
                    reset_5h=None,
                    reset_7d=None,
                ),
                now=timezone.now(),
            )
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

        with (
            routed_by(route_config(_SKILL, CLAUDE_SDK)),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=[_SKILL]),
            patch("teatree.llm.credentials.read_pass", side_effect=lambda path: path),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            scripted_sdk([_SESSION_REFUSAL]),
        ):
            attempt = run_agent(task, phase="coding", overlay_skill_metadata=SkillMetadata())

        task.refresh_from_db()
        assert attempt.error.startswith(f"{LIMIT_PARKED_PREFIX}rotating")
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before <= timezone.now()
        assert AnthropicActivePick.objects.pick_for("oauth", "") == second
        assert AnthropicTokenUsage.objects.get(pass_path=first).is_exhausted
        assert not AgentRouteAvailability.objects.exists()
        assert attempt.account == first
