"""A Codex candidate with no login in its private home is skipped with the command that fixes it."""

import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import teatree.agents.runner as runner_mod
from teatree.agents import codex_app_server_options
from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    MANAGED,
    failing_session,
    register_stub_harnesses,
    route_config,
    routed_by,
)

_CODEX = "codex_app_server"


def _refusing_the_login(read: Callable[..., object]) -> Callable[..., object]:
    def guarded(path: Path, *args: object, **kwargs: object) -> object:
        if path.name == "auth.json":
            raise AssertionError(read.__name__)
        return read(path, *args, **kwargs)

    return guarded


class TestCodexLoginHealth(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, CLAUDE_LIKE)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for patcher in (
            patch.multiple(
                codex_app_server_options, running_in_container=lambda: False, container_is_the_sandbox=lambda: False
            ),
            patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=False),
            patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {"T3_CODEX_HOME": str(self.home)}),
        ):
            self.enterContext(patcher)
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")

    def _resolve(self) -> DispatchHarness:
        with routed_by(route_config("route-skill", _CODEX, CLAUDE_LIKE)):
            return resolve_dispatch_harness(self.task, phase="debugging", skills=["route-skill"])

    def test_a_missing_login_rejects_the_candidate_naming_the_import_command(self) -> None:
        dispatch = self._resolve()

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_LIKE, 1)
        assert "t3 codex auth import" in dispatch.rejected[0].reason

    def test_a_login_in_the_private_home_keeps_the_candidate_without_reading_it(self) -> None:
        (self.home / "auth.json").write_text('{"tokens": {"access_token": "sentinel-do-not-read"}}')

        with ExitStack() as stack:
            for reader in ("read_bytes", "read_text", "open"):
                stack.enter_context(patch.object(Path, reader, _refusing_the_login(getattr(Path, reader))))
            dispatch = self._resolve()

        assert (dispatch.name, dispatch.route_candidate_index) == (_CODEX, 0)


class TestTransportHold(TestCase):
    def test_a_transport_failure_keeps_the_two_minute_hold(self) -> None:
        register_stub_harnesses(
            self,
            MANAGED,
            CLAUDE_LIKE,
            sessions={MANAGED: failing_session(HarnessFallbackError("dropped", kind=HarnessFallbackKind.TRANSPORT))},
        )
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")
        config = route_config("route-skill", MANAGED, CLAUDE_LIKE)

        def candidate_after(minutes: int) -> int | None:
            with (
                routed_by(config),
                patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=minutes)),
            ):
                return resolve_dispatch_harness(task, phase="debugging", skills=["route-skill"]).route_candidate_index

        with (
            routed_by(config),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["route-skill"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())

        assert (candidate_after(1), candidate_after(3)) == (1, 0)
