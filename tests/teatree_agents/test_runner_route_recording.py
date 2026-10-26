"""A routed candidate refused for credit has spent its key, not the route: rotate, else wait for the plans."""

import contextlib
import os
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness as harness_mod
from teatree.agents.runner import run_agent
from teatree.core.models import (
    LIMIT_PARKED_PREFIX,
    AgentRouteAvailability,
    AnthropicTokenUsage,
    Session,
    Task,
    TaskAttempt,
)
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
