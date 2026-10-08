"""A Codex quota or auth failure, mid-turn or not, holds the candidate and its task out for an hour."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import teatree.agents.runner as runner_mod
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task, TaskAttempt
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    MANAGED,
    failing_session,
    register_stub_harnesses,
    route_config,
    routed_by,
    scripted_session,
)
from tests.teatree_agents._sdk_fake import FakeHarnessSession, assistant_tool_use, result_message

_HOUR = timedelta(minutes=59)
_ROUTE = route_config("route-skill", MANAGED, CLAUDE_LIKE)


_QUOTA = "ChatGPT quota exhausted: usage limit reached (429)"
_QUOTA_OUTCOME = scripted_session(
    [assistant_tool_use(), result_message(is_error=True, subtype="error_during_execution", result=_QUOTA)]
)


def _fault(kind: HarnessFallbackKind, *, mid_turn: bool) -> type[FakeHarnessSession]:
    return failing_session(HarnessFallbackError(f"{kind.value} failure", kind=kind, side_effects_started=mid_turn))


class TestCodexHold(TestCase):
    def _run(self, codex_session: type[FakeHarnessSession]) -> tuple[Task, TaskAttempt]:
        register_stub_harnesses(self, MANAGED, CLAUDE_LIKE, sessions={MANAGED: codex_session})
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")
        with (
            routed_by(_ROUTE),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["route-skill"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())
        task.refresh_from_db()
        return task, attempt

    def _next_candidate(self, task: Task, *, later: timedelta = timedelta(minutes=10)) -> int | None:
        with routed_by(_ROUTE), patch("django.utils.timezone.now", return_value=timezone.now() + later):
            return resolve_dispatch_harness(task, phase="debugging", skills=["route-skill"]).route_candidate_index

    def _assert_held_for_an_hour(self, kind: HarnessFallbackKind) -> None:
        task, _attempt = self._run(_fault(kind, mid_turn=True))

        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before >= timezone.now() + _HOUR
        assert self._next_candidate(task) == 1

    def test_an_auth_failure_after_side_effects_holds_the_candidate_and_parks_the_task_for_an_hour(self) -> None:
        self._assert_held_for_an_hour(HarnessFallbackKind.AUTH)

    def test_a_quota_failure_after_side_effects_holds_the_candidate_and_parks_the_task_for_an_hour(self) -> None:
        self._assert_held_for_an_hour(HarnessFallbackKind.QUOTA)

    def test_an_exhausted_quota_after_side_effects_holds_the_candidate_and_parks_the_task_for_an_hour(self) -> None:
        self._assert_held_for_an_hour(HarnessFallbackKind.QUOTA_EXHAUSTED)

    def test_a_quota_outcome_that_already_made_tool_calls_holds_the_same_way(self) -> None:
        task, _attempt = self._run(_QUOTA_OUTCOME)

        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before >= timezone.now() + _HOUR
        assert self._next_candidate(task) == 1

    def test_a_quota_failure_before_any_side_effect_holds_the_candidate_for_an_hour(self) -> None:
        _task, attempt = self._run(_fault(HarnessFallbackKind.QUOTA, mid_turn=False))

        assert attempt.route_candidate_index == 1
        assert self._next_candidate(attempt.task) == 1

    def test_a_transport_failure_after_side_effects_keeps_the_short_hold(self) -> None:
        task, _attempt = self._run(_fault(HarnessFallbackKind.TRANSPORT, mid_turn=True))

        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before < timezone.now() + timedelta(minutes=1)
        assert self._next_candidate(task, later=timedelta(0)) == 0
