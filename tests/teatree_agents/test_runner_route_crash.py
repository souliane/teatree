"""An unhandled crash on a routed run is one recorded FAILED attempt, so the next dispatch moves on."""

from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task, TaskAttempt
from teatree.core.models.config_setting import ConfigSetting
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    CLAUDE_SDK,
    MANAGED,
    CrashingSession,
    register_stub_harnesses,
    route_config,
    routed_by,
    two_healthy_oauth_accounts,
)
from tests.teatree_agents._sdk_fake import fake_sdk


class TestRoutedCrash(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, MANAGED, CLAUDE_LIKE, sessions={MANAGED: CrashingSession})
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")

    def _run(self) -> TaskAttempt:
        with (
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["route-skill"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            return run_agent(self.task, phase="debugging", overlay_skill_metadata=SkillMetadata())

    def test_a_routed_crash_records_one_failed_attempt_with_its_route_and_fails_the_task(self) -> None:
        with routed_by(route_config("route-skill", MANAGED, CLAUDE_LIKE)):
            attempt = self._run()

        self.task.refresh_from_db()
        assert list(self.task.attempts.all()) == [attempt]
        assert (attempt.route_candidate_index, attempt.route_source_skill) == (0, "route-skill")
        assert (attempt.outcome, attempt.failure_kind) == (TaskAttempt.Outcome.CRASH, "harness_crash")
        assert "invalid app-server response schema" in attempt.error
        assert self.task.status == Task.Status.FAILED

    def test_an_unrouted_crash_still_propagates_to_the_caller(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", MANAGED)

        with routed_by(route_config("other-skill", CLAUDE_LIKE)), pytest.raises(RuntimeError, match="schema"):
            self._run()

        assert not self.task.attempts.exists()


class TestARoutedCrashKeepsItsSigningAccount(TestCase):
    def test_the_failed_attempt_names_the_account_the_run_was_signed_with(self) -> None:
        first, _second = two_healthy_oauth_accounts()
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

        with (
            routed_by(route_config("ac-python", CLAUDE_SDK)),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["ac-python"]),
            patch("teatree.llm.credentials.read_pass", side_effect=lambda path: path),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            fake_sdk([]),
            patch.object(harness_mod, "ClaudeSDKClient", lambda **_: CrashingSession([])),
        ):
            attempt = run_agent(task, phase="coding", overlay_skill_metadata=SkillMetadata())

        task.refresh_from_db()
        assert (attempt.outcome, attempt.account) == (TaskAttempt.Outcome.CRASH, first)
        assert task.status == Task.Status.FAILED
