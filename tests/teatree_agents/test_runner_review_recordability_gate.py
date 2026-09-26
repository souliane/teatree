"""The dispatch chokepoint refuses an unrecordable review BEFORE the model runs.

The defect is ORDERING, so a probe that merely counts spawns is not enough: the harness
double here RAISES when it is opened, so a gate that fires one instruction too late turns
the test red with the constructor's own error rather than with a soft assertion. What the
old order cost is the reason — 53 full Opus reviews paid for, then discarded on a
precondition two DB reads could have answered first.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.agents import harness as harness_mod
from teatree.agents import runner as runner_mod
from teatree.agents import skill_assurance as skill_assurance_mod
from teatree.agents import skill_injection as skill_injection_mod
from teatree.agents.runner import run_agent
from teatree.core.modelkit.task_failure_taxonomy import REVIEW_UNRECORDABLE_PREFIX, FailureKind
from teatree.core.models import Session, Task, Ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

_SLUG = "souliane/teatree"
_PR_ID = 225
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"


class _HarnessMustNotOpenError(AssertionError):
    """Raised by the double the moment the runner tries to spawn a child."""


class _ReviewDispatchProbe(TestCase):
    """Drives ``run_agent`` for a reviewing task with the SDK child replaced by a double."""

    #: When True the double RAISES instead of streaming — the refusal tests use it so a
    #: late gate fails with the spawn itself rather than with a spend assertion.
    harness_must_not_open = False

    def setUp(self) -> None:
        self.spawned: list[object] = []

    def _dispatch(self, ticket: Ticket, *, phase: str = "reviewing") -> Task:
        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            self.spawned.append(options)
            if self.harness_must_not_open:
                raise _HarnessMustNotOpenError(phase)
            return FakeHarnessSession(success_stream({"summary": "ok"}))

        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        snapshot = runner_mod.TaskUsage(turns=0, cost_usd=0.0)
        with TemporaryDirectory() as directory:
            # Keep this gate test independent of the host's installed skills.
            for skill_name in (
                "code-review",
                "interactive",
                "internals",
                "slack-formatting",
                "rules",
                "platforms",
                "review",
                "code",
            ):
                skill_file = Path(directory) / skill_name / "SKILL.md"
                skill_file.parent.mkdir()
                skill_file.write_text(f"# {skill_name}\nFollow the test instructions.\n", encoding="utf-8")
            skill_dirs = [Path(directory)]
            with (
                patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(harness_mod, "ClaudeSDKClient", _make_client),
                patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
                patch.object(skill_injection_mod, "harness_skills_dirs", return_value=skill_dirs),
                patch.object(skill_assurance_mod, "harness_skills_dirs", return_value=skill_dirs),
            ):
                run_agent(task, phase=phase, overlay_skill_metadata={})
        task.refresh_from_db()
        return task

    @staticmethod
    def _reviewer_ticket(*, extra: dict[str, object] | None = None) -> Ticket:
        return Ticket.objects.create(
            issue_url=_PR_URL,
            overlay="t3-teatree",
            role=Ticket.Role.REVIEWER,
            extra=extra or {},
        )


class TestAnUnrecordableReviewIsRefusedBeforeTheHarnessOpens(_ReviewDispatchProbe):
    harness_must_not_open = True

    def test_no_agent_child_is_spawned_for_a_pull_request_with_no_recorded_head(self) -> None:
        task = self._dispatch(self._reviewer_ticket())

        assert self.spawned == [], "a review whose verdict could not be recorded must never open the harness"
        assert task.status == Task.Status.FAILED

    def test_the_recorded_attempt_names_the_cause_and_bills_no_spend(self) -> None:
        task = self._dispatch(self._reviewer_ticket())

        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.error.startswith(REVIEW_UNRECORDABLE_PREFIX)
        assert f"{_SLUG}#{_PR_ID}" in attempt.error
        # Refused before the harness opened, so no turn was billed — NULL, not zero.
        assert attempt.cost_usd is None
        assert attempt.num_turns is None

    def test_the_attempt_carries_the_named_failure_kind_rather_than_unclassified(self) -> None:
        task = self._dispatch(self._reviewer_ticket())

        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.failure_kind == FailureKind.REVIEW_UNRECORDABLE


class TestARecordableReviewStillDispatches(_ReviewDispatchProbe):
    def test_a_reviewer_ticket_carrying_its_head_still_spawns_its_reviewer(self) -> None:
        self._dispatch(self._reviewer_ticket(extra={"reviewed_sha": _HEAD}))

        assert self.spawned, "a review whose head IS recorded must dispatch exactly as before"
