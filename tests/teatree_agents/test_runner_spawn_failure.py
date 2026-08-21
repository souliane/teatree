"""A dispatch whose agent process never starts is recorded as itself, not as 40 SDK frames (#4301).

The observed failure: ``CLIConnectionError: Failed to start Claude Code: [Errno 7] Argument
list too long`` was stored verbatim as a full traceback, so the repair-halt built from it
asked the operator to adjudicate a ticket whose content the child never read. These drive
the REAL :class:`~teatree.agents.harness.ClaudeSdkHarness` (only the SDK client is faked),
so the naming happens where the spawn happens.
"""

from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task, Ticket
from teatree.failure_signatures import is_spawn_failure
from tests.factories import planned_ticket


def _raising_client(exc: Exception) -> Any:
    def _make_client(*, options: Any = None, **_: object) -> Any:
        raise exc

    return _make_client


class TestSpawnFailureIsRecordedByName(TestCase):
    def setUp(self) -> None:
        self.ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        self.session = Session.objects.create(ticket=self.ticket, agent_id="testing")
        self.task = Task.objects.create(
            ticket=self.ticket, session=self.session, phase="testing", status=Task.Status.CLAIMED
        )

    def _dispatch(self, exc: Exception) -> Any:
        with (
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _raising_client(exc)),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            return run_agent(self.task, phase="testing", overlay_skill_metadata={})

    def test_an_e2big_death_is_named_rather_than_traced(self) -> None:
        attempt = self._dispatch(
            RuntimeError("Failed to start Claude Code: [Errno 7] Argument list too long: '.../claude'")
        )

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert "agent could not be spawned" in attempt.error
        assert "E2BIG" in attempt.error
        assert "Traceback" not in attempt.error

    def test_the_recorded_error_reaches_the_spawn_failure_classifier(self) -> None:
        # The repair-halt reads this text to choose its question, so the recorder and
        # the classifier must agree — a naming the classifier misses changes nothing.
        attempt = self._dispatch(RuntimeError("Failed to start Claude Code: [Errno 7] Argument list too long"))
        assert is_spawn_failure(attempt.error)

    def test_the_measured_size_is_reported(self) -> None:
        attempt = self._dispatch(RuntimeError("Failed to start Claude Code: [Errno 7] Argument list too long"))
        assert "spawn payload" in attempt.error

    def test_any_other_startup_failure_is_left_to_the_generic_path(self) -> None:
        # Only E2BIG is re-labelled; a missing binary must keep its own diagnosis.
        with pytest.raises(RuntimeError):
            self._dispatch(RuntimeError("Failed to start Claude Code: [Errno 2] No such file or directory"))
